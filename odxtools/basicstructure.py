# SPDX-License-Identifier: MIT
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

from .complexdop import ComplexDop
from .dataobjectproperty import DataObjectProperty
from .decodestate import DecodeState
from .encodestate import EncodeState
from .exceptions import DecodeError, EncodeError, OdxWarning, odxassert, odxraise
from .nameditemlist import NamedItemList
from .odxlink import OdxDocFragment, OdxLinkDatabase, OdxLinkId
from .odxtypes import ParameterDict, ParameterValue, ParameterValueDict
from .parameters.codedconstparameter import CodedConstParameter
from .parameters.createanyparameter import create_any_parameter_from_et
from .parameters.lengthkeyparameter import LengthKeyParameter
from .parameters.matchingrequestparameter import MatchingRequestParameter
from .parameters.nrcconstparameter import NrcConstParameter
from .parameters.parameter import Parameter
from .parameters.parameterwithdop import ParameterWithDOP
from .parameters.physicalconstantparameter import PhysicalConstantParameter
from .parameters.tablekeyparameter import TableKeyParameter
from .utils import dataclass_fields_asdict

if TYPE_CHECKING:
    from .diaglayer import DiagLayer


@dataclass
class BasicStructure(ComplexDop):
    parameters: NamedItemList[Parameter]
    byte_size: Optional[int]

    @staticmethod
    def from_et(et_element: ElementTree.Element,
                doc_frags: List[OdxDocFragment]) -> "BasicStructure":
        """Read a BASIC-STRUCTURE."""
        kwargs = dataclass_fields_asdict(ComplexDop.from_et(et_element, doc_frags))

        parameters = NamedItemList([
            create_any_parameter_from_et(et_parameter, doc_frags)
            for et_parameter in et_element.iterfind("PARAMS/PARAM")
        ])

        byte_size_str = et_element.findtext("BYTE-SIZE")
        byte_size = int(byte_size_str) if byte_size_str is not None else None

        return BasicStructure(parameters=parameters, byte_size=byte_size, **kwargs)

    def get_static_bit_length(self) -> Optional[int]:
        # Explicit size was specified
        if self.byte_size:
            return 8 * self.byte_size

        cursor = 0
        length = 0
        for param in self.parameters:
            param_bit_length = param.get_static_bit_length()
            if param_bit_length is None:
                # We were not able to calculate a static bit length
                return None
            elif param.byte_position is not None:
                bit_pos = param.bit_position or 0
                byte_pos = param.byte_position or 0
                cursor = byte_pos * 8 + bit_pos

            cursor += param_bit_length
            length = max(length, cursor)

        # Round up to account for padding bits
        return ((length + 7) // 8) * 8

    def coded_const_prefix(self, request_prefix: bytes = b'') -> bytes:
        prefix = b''
        encode_state = EncodeState(prefix, parameter_values={}, triggering_request=request_prefix)
        for p in self.parameters:
            if isinstance(p, (CodedConstParameter, NrcConstParameter, MatchingRequestParameter,
                              PhysicalConstantParameter)):
                encode_state.coded_message = p.encode_into_pdu(encode_state)
            else:
                break
        return encode_state.coded_message

    @property
    def required_parameters(self) -> List[Parameter]:
        """Return the list of parameters which are required for
        encoding the structure."""
        return [p for p in self.parameters if p.is_required]

    @property
    def free_parameters(self) -> List[Parameter]:
        """Return the list of parameters which can be freely specified by
        the user when encoding the structure.

        This means all required parameters plus the parameters that
        can be omitted minus those which are implicitly specified by
        the corresponding request (in the case of responses).

        """
        result: List[Parameter] = []
        for param in self.parameters:
            if not param.is_settable:
                continue
            result.append(param)

        return result

    def print_free_parameters_info(self) -> None:
        """Return a human readable description of the structure's
        free parameters.
        """
        from .parameterinfo import parameter_info

        print(parameter_info(self.free_parameters), end="")

    def convert_physical_to_internal(self,
                                     param_value: ParameterValue,
                                     triggering_coded_request: Optional[bytes],
                                     is_end_of_pdu: bool = True) -> bytes:

        if not isinstance(param_value, dict):
            raise EncodeError(
                f"Expected a dictionary for the values of structure {self.short_name}, "
                f"got {type(param_value)}")

        encode_state = EncodeState(
            b'',
            dict(param_value),
            triggering_request=triggering_coded_request,
            is_end_of_pdu=False,
        )

        for param in self.parameters:
            if param == self.parameters[-1]:
                # The last parameter is at the end of the PDU if the
                # structure itself is at the end of the PDU. TODO:
                # This assumes that the last parameter specified in
                # the ODX is located last in the PDU...
                encode_state.is_end_of_pdu = is_end_of_pdu

            encode_state.coded_message = param.encode_into_pdu(encode_state)

        if self.byte_size is not None and len(encode_state.coded_message) < self.byte_size:
            # Padding bytes needed
            encode_state.coded_message = encode_state.coded_message.ljust(self.byte_size, b"\0")

        # encode the length- and table keys. This cannot be done above
        # because we allow these to be defined implicitly (i.e. they
        # are defined by their respective users)
        for param in self.parameters:
            if not isinstance(param, (LengthKeyParameter, TableKeyParameter)):
                # the current parameter is neither a length- nor a table key
                continue

            # Encode the key parameter into the message
            encode_state.coded_message = param.encode_into_pdu(encode_state)

        # Assert that length is as expected
        self._validate_coded_message(encode_state.coded_message)

        return bytearray(encode_state.coded_message)

    def _validate_coded_message(self, coded_message: bytes) -> None:

        if self.byte_size is not None:
            # We definitely broke something if we didn't respect the explicit byte_size
            odxassert(
                len(coded_message) == self.byte_size,
                "Verification of coded message {coded_message.hex()} failed: Incorrect size.")
            # No need to check further
            return

        bit_length = self.get_static_bit_length()

        if bit_length is None:
            # Nothing to check
            return

        if len(coded_message) * 8 != bit_length:
            # We may have broke something
            # but it could be that bit_length was mis calculated and not the actual bytes are wrong
            # Could happen with overlapping parameters and parameters with gaps
            warnings.warn(
                f"Verification of coded message '{coded_message.hex()}' possibly failed: Size may be incorrect.",
                OdxWarning,
                stacklevel=1)

    def convert_physical_to_bytes(self,
                                  param_values: ParameterValue,
                                  encode_state: EncodeState,
                                  bit_position: int = 0) -> bytes:
        if not isinstance(param_values, dict):
            raise EncodeError(
                f"Expected a dictionary for the values of structure {self.short_name}, "
                f"got {type(param_values)}")
        if bit_position != 0:
            raise EncodeError("Structures must be aligned, i.e. bit_position=0, but "
                              f"{self.short_name} was passed the bit position {bit_position}")
        return self.convert_physical_to_internal(
            param_values,
            triggering_coded_request=encode_state.triggering_request,
            is_end_of_pdu=encode_state.is_end_of_pdu,
        )

    def convert_bytes_to_physical(self,
                                  decode_state: DecodeState,
                                  bit_position: int = 0) -> Tuple[ParameterValue, int]:
        if bit_position != 0:
            raise DecodeError("Structures must be aligned, i.e. bit_position=0, but "
                              f"{self.short_name} was passed the bit position {bit_position}")
        byte_code = decode_state.coded_message[decode_state.cursor_position:]
        inner_decode_state = DecodeState(
            coded_message=byte_code, parameter_values={}, cursor_position=0)

        for parameter in self.parameters:
            value, cursor_position = parameter.decode_from_pdu(inner_decode_state)

            inner_decode_state.parameter_values[parameter.short_name] = value
            inner_decode_state = DecodeState(
                coded_message=byte_code,
                parameter_values=inner_decode_state.parameter_values,
                cursor_position=max(inner_decode_state.cursor_position, cursor_position),
            )

        return inner_decode_state.parameter_values, decode_state.cursor_position + inner_decode_state.cursor_position

    def encode(self, coded_request: Optional[bytes] = None, **params: ParameterValue) -> bytes:
        """
        Composes an UDS message as bytes for this service.
        Parameters:
        ----------
        coded_request: bytes
            coded request (only needed when encoding a response)
        params: dict
            Parameters of the RPC as mapping from SHORT-NAME of the parameter to the value
        """
        return self.convert_physical_to_internal(
            params,  # type: ignore[arg-type]
            triggering_coded_request=coded_request,
            is_end_of_pdu=True)

    def decode(self, message: bytes) -> ParameterValueDict:
        # dummy decode state to be passed to convert_bytes_to_physical
        decode_state = DecodeState(parameter_values={}, coded_message=message, cursor_position=0)
        param_values, cursor_position = self.convert_bytes_to_physical(decode_state)
        if not isinstance(param_values, dict):
            odxraise(f"Decoding a structure must result in a dictionary of parameter "
                     f"values (is {type(param_values)})")
        if len(message) != cursor_position:
            warnings.warn(
                f"The message {message.hex()} is longer than could be parsed."
                f" Expected {cursor_position} but got {len(message)}.",
                DecodeError,
                stacklevel=1,
            )
        return param_values

    def parameter_dict(self) -> ParameterDict:
        """
        Returns a dictionary with all parameter short names as keys.

        The values are parameters for simple types or a nested dict for structures.
        """
        from .structure import Structure
        odxassert(
            all(not isinstance(p, ParameterWithDOP) or isinstance(p.dop, DataObjectProperty) or
                isinstance(p.dop, Structure) for p in self.parameters))
        param_dict: ParameterDict = {
            p.short_name: p
            for p in self.parameters
            if not isinstance(p, ParameterWithDOP) or not isinstance(p.dop, Structure)
        }
        param_dict.update({
            struct_param.short_name: struct_param.dop.parameter_dict()
            for struct_param in self.parameters
            if isinstance(struct_param, ParameterWithDOP) and
            isinstance(struct_param.dop, BasicStructure)
        })
        return param_dict

    def _build_odxlinks(self) -> Dict[OdxLinkId, Any]:
        result = super()._build_odxlinks()

        for p in self.parameters:
            result.update(p._build_odxlinks())

        return result

    def _resolve_odxlinks(self, odxlinks: OdxLinkDatabase) -> None:
        """Recursively resolve any references (odxlinks or sn-refs)"""
        super()._resolve_odxlinks(odxlinks)

        for p in self.parameters:
            p._resolve_odxlinks(odxlinks)

    def _resolve_snrefs(self, diag_layer: "DiagLayer") -> None:
        """Recursively resolve any references (odxlinks or sn-refs)"""
        super()._resolve_snrefs(diag_layer)

        for p in self.parameters:
            p._resolve_snrefs(diag_layer)
