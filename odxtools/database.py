# SPDX-License-Identifier: MIT
from itertools import chain
from pathlib import Path
from typing import List, Optional, Tuple
from xml.etree import ElementTree
from zipfile import ZipFile

from .comparamspec import ComparamSpec
from .comparamsubset import ComparamSubset
from .diaglayer import DiagLayer
from .diaglayercontainer import DiagLayerContainer
from .globals import logger
from .nameditemlist import NamedItemList, short_name_as_key
from .odxlink import OdxLinkDatabase


def version(v: str) -> Tuple[int, ...]:
    return tuple(map(int, (v.split("."))))


class Database:
    """This class internalizes the diagnostic database for various ECUs
    described by a collection of ODX files which are usually collated
    into a single PDX file.
    """

    def __init__(self,
                 *,
                 pdx_zip: Optional[ZipFile] = None,
                 odx_d_file_name: Optional[str] = None) -> None:

        if pdx_zip is None and odx_d_file_name is None:
            # create an empty database object
            self._diag_layer_containers = NamedItemList[DiagLayerContainer]()
            self._comparam_subsets = NamedItemList[ComparamSubset]()
            self._comparam_specs = NamedItemList[ComparamSpec]()
            return

        if pdx_zip is not None and odx_d_file_name is not None:
            raise TypeError("The 'pdx_zip' and 'odx_d_file_name' parameters are mutually exclusive")

        documents: List[ElementTree.Element] = []
        if pdx_zip is not None:
            names = list(pdx_zip.namelist())
            names.sort()
            for zip_member in names:
                # file name can end with .odx, .odx-d, .odx-c, .odx-cs, .odx-e, .odx-f, .odx-fd, .odx-m, .odx-v
                # We could test for all that, or just make sure suffix starts with .odx
                if Path(zip_member).suffix.startswith(".odx"):
                    logger.info(f"Processing the file {zip_member}")
                    d = pdx_zip.read(zip_member)
                    root = ElementTree.fromstring(d)
                    documents.append(root)

        elif odx_d_file_name is not None:
            documents.append(ElementTree.parse(odx_d_file_name).getroot())

        dlcs: List[DiagLayerContainer] = []
        comparam_subsets: List[ComparamSubset] = []
        comparam_specs: List[ComparamSpec] = []
        for root in documents:
            # ODX spec version
            model_version = version(root.attrib.get("MODEL-VERSION", "2.0"))
            dlc = root.find("DIAG-LAYER-CONTAINER")
            if dlc is not None:
                dlcs.append(DiagLayerContainer.from_et(dlc, []))
            # In ODX 2.0 there was only COMPARAM-SPEC
            # In ODX 2.2 content of COMPARAM-SPEC was renamed to COMPARAM-SUBSET
            # and COMPARAM-SPEC becomes a container for PROT-STACKS
            # and a PROT-STACK references a list of COMPARAM-SUBSET
            if model_version >= version("2.2"):
                subset = root.find("COMPARAM-SUBSET")
                if subset is not None:
                    comparam_subsets.append(ComparamSubset.from_et(subset, []))
            else:
                spec = root.find("COMPARAM-SPEC")
                if spec is not None:
                    comparam_specs.append(ComparamSpec.from_et(spec, []))

        self._diag_layer_containers = NamedItemList(dlcs)
        self._diag_layer_containers.sort(key=short_name_as_key)
        self._comparam_subsets = NamedItemList(comparam_subsets)
        self._comparam_subsets.sort(key=short_name_as_key)
        self._comparam_specs = NamedItemList(comparam_specs)
        self._comparam_specs.sort(key=short_name_as_key)

        self.refresh()

    def refresh(self) -> None:
        # Create wrapper objects
        self._diag_layers = NamedItemList(
            chain(*[dlc.diag_layers for dlc in self.diag_layer_containers]))

        self._protocols = NamedItemList(
            chain(*[dlc.protocols for dlc in self.diag_layer_containers]))

        self._ecus = NamedItemList(chain(*[dlc.ecu_variants for dlc in self.diag_layer_containers]))

        # Build odxlinks
        self._odxlinks = OdxLinkDatabase()

        for subset in self.comparam_subsets:
            self._odxlinks.update(subset._build_odxlinks())

        for dlc in self.diag_layer_containers:
            self._odxlinks.update(dlc._build_odxlinks())

        # Resolve ODXLINK references
        for subset in self.comparam_subsets:
            subset._resolve_odxlinks(self._odxlinks)

        for dlc in self.diag_layer_containers:
            dlc._resolve_odxlinks(self._odxlinks)

        # let the diaglayers sort out the inherited objects and the
        # short name references
        for dlc in self.diag_layer_containers:
            dlc._finalize_init(self._odxlinks)

    @property
    def odxlinks(self) -> OdxLinkDatabase:
        """A map from odx_id to object"""
        return self._odxlinks

    @property
    def protocols(self) -> NamedItemList[DiagLayer]:
        """
        All protocols defined by this database
        """
        return self._protocols

    @property
    def ecus(self) -> NamedItemList[DiagLayer]:
        """ECU-variants defined in the database"""
        return self._ecus

    @property
    def diag_layers(self) -> NamedItemList[DiagLayer]:
        """All diagnostic layers defined in the database"""
        return self._diag_layers

    @property
    def diag_layer_containers(self) -> NamedItemList[DiagLayerContainer]:
        return self._diag_layer_containers

    @diag_layer_containers.setter
    def diag_layer_containers(self, value: NamedItemList[DiagLayerContainer]) -> None:
        self._diag_layer_containers = value

    @property
    def comparam_subsets(self) -> NamedItemList[ComparamSubset]:
        return self._comparam_subsets

    @property
    def comparam_specs(self) -> NamedItemList[ComparamSpec]:
        return self._comparam_specs
