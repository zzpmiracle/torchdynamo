import collections
import dataclasses
from typing import Any

from . import variables
from .bytecode_transformation import create_instruction
from .codegen import PyCodegen
from .source import Source
from .variables.base import VariableTracker


@dataclasses.dataclass
class MutableSideEffects:
    """
    VariableTracker.mutable_local marker to indicate a list passed as
    an input that if we mutate we need to re-apply those mutations after
    the graph runs.
    """

    source: Source
    is_modified: bool


class SideEffects(object):
    """
    Track side effects (list mutation, setattr, etc) that need to be
    applied after an FX graph is run.
    """

    def __init__(self, id_to_variable=None, keepalive=None):
        super(SideEffects, self).__init__()
        self.id_to_variable = id_to_variable or collections.OrderedDict()
        self.keepalive = keepalive or []

    def clone(self):
        """Create a shallow copy"""
        return self.__class__(
            id_to_variable=collections.OrderedDict(self.id_to_variable),
            keepalive=list(self.keepalive),
        )

    def __contains__(self, item):
        return id(item) in self.id_to_variable

    def __getitem__(self, item):
        return self.id_to_variable[id(item)]

    def _track_obj(
        self,
        source: Source,
        item: Any,
        variable: VariableTracker,
    ):
        """Start tracking a new variable for mutation"""
        variable = variable.clone(mutable_local=MutableSideEffects(source, False))
        self.id_to_variable[id(item)] = variable
        self.keepalive.append(item)
        return variable

    track_list = _track_obj
    track_dict = _track_obj

    def mutation(self, oldvar, newvar):
        return newvar.clone(
            mutable_local=MutableSideEffects(oldvar.mutable_local.source, True)
        )

    def apply(self, fn):
        self.id_to_variable = collections.OrderedDict(
            (k, VariableTracker.apply(fn, v)) for k, v in self.id_to_variable.items()
        )

    def codegen(self, cg: PyCodegen):
        modified_vars = [
            var for var in self.id_to_variable.values() if var.mutable_local.is_modified
        ]

        for var in modified_vars:
            assert cg.tempvars.get(var) is None
            if var in cg.tempvars:
                # subsequent usage should point to the original variable
                cg(var.mutable_local.source)
                cg.add_cache(var)

        suffixes = []
        for var in modified_vars:
            if isinstance(var, variables.ListVariable):
                # old[:] = new
                cg(var, allow_cache=False)
                cg(var.mutable_local.source)
                cg.extend_output(
                    [
                        cg.create_load_const(None),
                        cg.create_load_const(None),
                        create_instruction("BUILD_SLICE", 2),
                    ]
                )
                suffixes.append([create_instruction("STORE_SUBSCR")])
            elif isinstance(var, variables.ConstDictVariable):
                cg.tx.output.update_co_names("clear")
                cg.tx.output.update_co_names("update")

                cg(var.mutable_local.source)
                cg.extend_output([create_instruction("LOAD_METHOD", "update")])
                cg(var, allow_cache=False)

                cg(var.mutable_local.source)
                cg.extend_output([create_instruction("LOAD_METHOD", "clear")])

                suffixes.append(
                    [
                        create_instruction("CALL_METHOD", 0),  # clear
                        create_instruction("POP_TOP"),
                        create_instruction("CALL_METHOD", 1),  # update
                        create_instruction("POP_TOP"),
                    ]
                )
            else:
                assert False, type(var)

        # do all the actual mutations at the very end to handle dependencies
        for suffix in reversed(suffixes):
            cg.extend_output(suffix)

    def is_empty(self):
        return not any(
            var.mutable_local.is_modified for var in self.id_to_variable.values()
        )