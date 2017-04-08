
import logging
from collections import defaultdict
from itertools import count

from simuvex import BP, BP_BEFORE, BP_AFTER
from simuvex.s_variable import SimRegisterVariable, SimStackVariable

from ...analysis import Analysis, register_analysis
from ..forward_analysis import ForwardAnalysis, FunctionGraphVisitor
from ..code_location import CodeLocation
from .keyed_region import KeyedRegion
from .annotations import StackLocationAnnotation


l = logging.getLogger('angr.analyses.variable_recovery')


class VariableRecoveryState(object):
    """
    The abstract state of variable recovery analysis.
    """

    def __init__(self, variable_manager, arch, concrete_states, stack_variables=None, register_variables=None):
        self.variable_manager = variable_manager
        self.arch = arch
        self._concrete_states = concrete_states

        self._state_per_instruction = { }
        if stack_variables is not None:
            self._stack_variables = stack_variables
        else:
            self._stack_variables = KeyedRegion()
        if register_variables is not None:
            self._register_variables = register_variables
        else:
            self._register_variables = KeyedRegion()

        # register callbacks
        self._register_callbacks(self.concrete_states)

    def __repr__(self):
        return "<VRAbstractState: %d register variables, %d stack variables>" % (len(self._register_variables), len(self._stack_variables))

    @property
    def concrete_states(self):
        return self._concrete_states

    @concrete_states.setter
    def concrete_states(self, v):
        self._concrete_states = v

        # register callbacks
        self._register_callbacks(self._concrete_states)

    def get_concrete_state(self, addr):
        """

        :param addr:
        :return:
        """

        for s in self.concrete_states:
            if s.ip._model_concrete.value == addr:
                return s

        return None

    def copy(self, copy_substates=True):

        state = VariableRecoveryState(self.variable_manager,
                                      self.arch,
                                      self._concrete_states,
                                      stack_variables=self._stack_variables.copy(),
                                      register_variables=self._register_variables.copy(),
                                      )

        if copy_substates:
            state._state_per_instruction = dict((k, v.copy()) for k, v in self._state_per_instruction.iteritems())

        return state

    def _register_callbacks(self, concrete_states):
        """

        :param concrete_states:
        :return:
        """

        for concrete_state in concrete_states:
            # clear existing breakpoints
            # TODO: all breakpoints are removed. Fix this later by only removing breakpoints that we added
            for bp_type in ('reg_read', 'reg_write', 'mem_read', 'mem_write', 'instruction'):
                concrete_state.inspect._breakpoints[bp_type] = [ ]

            concrete_state.inspect.add_breakpoint('instruction', BP(when=BP_AFTER, enabled=True, action=self._hook_instruction))
            concrete_state.inspect.add_breakpoint('reg_read', BP(when=BP_AFTER, enabled=True, action=self._hook_register_read))
            concrete_state.inspect.add_breakpoint('reg_write', BP(enabled=True, action=self._hook_register_write))
            concrete_state.inspect.add_breakpoint('mem_read', BP(when=BP_AFTER, enabled=True, action=self._hook_memory_read))
            concrete_state.inspect.add_breakpoint('mem_write', BP(enabled=True, action=self._hook_memory_write))

    def merge(self, other):
        """
        Merge two abstract states.

        :param VariableRecoveryState other: The other abstract state to merge.
        :return:                            The merged abstract state.
        :rtype:                             VariableRecoveryState
        """

        # TODO: finish it

        merged_concrete_states = self._merge_concrete_states(other)

        return VariableRecoveryState(self.variable_manager, self.arch, merged_concrete_states)

    def _merge_concrete_states(self, other):
        """

        :param VariableRecoveryState other:
        :return:
        :rtype:                             list
        """

        merged = [ ]

        for s in self.concrete_states:
            other_state = other.get_concrete_state(s.ip._model_concrete.value)
            if other_state is not None:
                s = s.merge(other_state)
            merged.append(s)

        return merged

    #
    # SimInspect callbacks
    #

    def _hook_instruction(self, state):
        """
        We have analyzed an instruction. Make a copy of the current abstract state.

        :param state:
        :return:
        """

        ins_addr = state.scratch.ins_addr
        self._state_per_instruction[ins_addr] = self.copy(copy_substates=False)

    def _hook_register_read(self, state):

        reg_read_offset = state.inspect.reg_read_offset
        reg_read_length = state.inspect.reg_read_length

        if reg_read_offset == state.arch.sp_offset and reg_read_length == state.arch.bits / 8:
            # TODO: make sure the sp is not overwritten by something that we are not tracking
            return

        #if reg_read_offset == state.arch.bp_offset and reg_read_length == state.arch.bits / 8:
        #    # TODO:

        var_offset = self._normalize_register_offset(reg_read_offset)
        if var_offset not in self._register_variables:
            # the variable being read doesn't exist before
            variable = SimRegisterVariable(reg_read_offset, reg_read_length,
                                           name=self.variable_manager.next_variable_name('register')
                                           )
            self._register_variables.add_variable(var_offset, variable)

    def _hook_register_write(self, state):

        reg_write_offset = state.inspect.reg_write_offset
        reg_write_expr = state.inspect.reg_write_expr
        reg_write_length = len(reg_write_expr) / 8

        # annotate it
        # reg_write_expr = reg_write_expr.annotate(VariableSourceAnnotation.from_state(state))

        state.inspect.reg_write_expr = reg_write_expr

        # create the variable
        variable = SimRegisterVariable(reg_write_offset, reg_write_length,
                                       name=self.variable_manager.next_variable_name('register')
                                       )
        var_offset = self._normalize_register_offset(reg_write_offset)
        self._register_variables.add_variable(var_offset, variable)

        # is it writing a pointer to a stack variable into the register?
        # e.g. lea eax, [ebp-0x40]
        stack_offset = self._addr_to_stack_offset(reg_write_expr)
        if stack_offset is not None:
            # it is!
            # unfortunately we don't know the size. We use size None for now.
            if stack_offset not in self._stack_variables:
                variable = SimStackVariable(stack_offset, None, base='bp',
                                            name=self.variable_manager.next_variable_name('stack')
                                            )
                self._stack_variables.add_variable(stack_offset, variable)

            base_offset = self._stack_variables.get_base_offset(stack_offset)
            assert base_offset is not None
            for variable in self._stack_variables.get_variables_by_offset(stack_offset):
                self.variable_manager.reference_at(variable, stack_offset - base_offset, self._codeloc_from_state(state))

    def _hook_memory_read(self, state):

        mem_read_address = state.inspect.mem_read_address
        mem_read_length = state.inspect.mem_read_length

        stack_offset = self._addr_to_stack_offset(mem_read_address)

        if stack_offset is None:
            # it's not a stack access
            # TODO:
            pass

        else:
            if stack_offset not in self._stack_variables:
                variable = SimStackVariable(stack_offset, mem_read_length, base='bp',
                                            name=self.variable_manager.next_variable_name('argument')
                                            )
                self._stack_variables.add_variable(stack_offset, variable)

            base_offset = self._stack_variables.get_base_offset(stack_offset)
            assert base_offset is not None
            for variable in self._stack_variables.get_variables_by_offset(stack_offset):
                self.variable_manager.read_from(variable, stack_offset - base_offset, self._codeloc_from_state(state))

    def _hook_memory_write(self, state):

        mem_write_address = state.inspect.mem_write_address
        mem_write_expr = state.inspect.mem_write_expr
        mem_write_length = len(mem_write_expr) / 8

        stack_offset = self._addr_to_stack_offset(mem_write_address)

        if stack_offset is None:
            # it's not a stack access
            # TODO:
            pass

        else:
            if stack_offset not in self._stack_variables:
                variable = SimStackVariable(stack_offset, mem_write_length, base='bp',
                                            name=self.variable_manager.next_variable_name('stack')
                                            )
                self._stack_variables.add_variable(stack_offset, variable)

            base_offset = self._stack_variables.get_base_offset(stack_offset)
            assert base_offset is not None
            for variable in self._stack_variables.get_variables_by_offset(stack_offset):
                self.variable_manager.write_to(variable, stack_offset - base_offset, self._codeloc_from_state(state))

    #
    # Util methods
    #

    def _normalize_register_offset(self, offset):

        # TODO:

        return offset

    @staticmethod
    def _codeloc_from_state(state):
        return CodeLocation(state.scratch.bbl_addr, state.scratch.stmt_idx, ins_addr=state.scratch.ins_addr)

    def _to_signed(self, n):

        if n >= 2 ** (self.arch.bits - 1):
            # convert it to a negative number
            return n - 2 ** self.arch.bits

        return n

    def _addr_to_stack_offset(self, addr):
        """
        Convert an address to a stack offset.

        :param claripy.ast.Base addr:  The address to convert from.
        :return:                       A stack offset if the addr comes from the stack pointer, or None if the address
                                       does not come from the stack pointer.
        """

        def _parse(addr):
            if addr.op == '__add__':
                # __add__ might have multiple arguments
                parsed = [ _parse(arg) for arg in addr.args ]
                annotated = [ True for annotated, _ in parsed if annotated is True ]
                if len(annotated) != 1:
                    # either nothing is annotated, or more than one element is annotated
                    raise ValueError()

                return True, sum([ offset for _, offset in parsed ])
            elif addr.op == '__sub__':
                # __sub__ might have multiple arguments

                parsed = [ _parse(arg) for arg in addr.args ]
                first_annotated, first_offset = parsed[0]
                if first_annotated is False:
                    # the first argument is not annotated. we don't support it.
                    raise ValueError()
                if any([ annotated for annotated, _ in parsed[1:] ]):
                    # more than one argument is annotated. we don't support it.
                    raise ValueError()

                return True, first_offset - sum([ offset for _, offset in parsed[1:] ])
            else:
                anno = next(iter(anno for anno in addr.annotations if isinstance(anno, StackLocationAnnotation)), None)
                if anno is None:
                    if addr.op == 'BVV':
                        return False, addr._model_concrete.value
                    raise ValueError()
                return True, anno.offset

        # find the annotated AST
        try: annotated, offset = _parse(addr)
        except ValueError: return None

        if not annotated:
            return None

        return self._to_signed(offset)


class VariableRecovery(ForwardAnalysis, Analysis):
    """
    Recover "variables" from a function.

    While variables play a very important role in programming, it does not really exist after compiling. However, we can
    still identify and recovery their counterparts in binaries. It is worth noting that not every variable in source
    code can be identified in binaries, and not every recognized variable in binaries have a corresponding variable in
    the original source code. In short, there is no guarantee that the variables we identified/recognized in a binary
    are the same variables in its source code.

    This analysis uses heuristics to identify and recover the following types of variables:
    - Register variables.
    - Stack variables.
    - Heap variables.

    This analysis takes a function as input, and performs a data-flow analysis to discover all places that are
    accessing variables.
    """

    def __init__(self, function):
        """

        :param knowledge.Function function:  The function to analyze.
        """

        function_graph_visitor = FunctionGraphVisitor(function)

        ForwardAnalysis.__init__(self, order_entries=True, allow_merging=True, allow_widening=False,
                                 graph_visitor=function_graph_visitor)

        self.function = function
        self._node_to_state = { }

        self._variable_accesses = defaultdict(list)
        self._insn_to_variable = defaultdict(list)
        self._variable_counters = {
            'register': count(),
            'stack': count(),
            'argument': count(),
        }

        self._analyze()

    #
    # Variable Manager
    #

    def next_variable_name(self, sort):
        if sort not in self._variable_counters:
            raise ValueError('Unsupported variable sort %s' % sort)

        if sort == 'register':
            prefix = "regvar"
        elif sort == 'stack':
            prefix = "var"
        elif sort == 'argument':
            prefix = 'arg'
        else:
            prefix = "memvar"

        return "%s_%d" % (prefix, self._variable_counters[sort].next())

    def write_to(self, variable, offset, location):
        self._variable_accesses[variable].append(('write', location))
        self._insn_to_variable[location.ins_addr].append((variable, offset))

    def read_from(self, variable, offset, location):
        self._variable_accesses[variable].append(('read', location))
        self._insn_to_variable[location.ins_addr].append((variable, offset))

    def reference_at(self, variable, offset, location):
        self._variable_accesses[variable].append(('reference', location))
        self._insn_to_variable[location.ins_addr].append((variable, offset))

    def find_variable_by_insn(self, ins_addr):
        if ins_addr not in self._insn_to_variable:
            return None

        return self._insn_to_variable[ins_addr][0]

    #
    # Main analysis routines
    #

    def _pre_analysis(self):
        pass

    def _pre_entry_handling(self, job):
        pass

    def _get_initial_abstract_state(self, node):

        concrete_state = self.project.factory.blank_state(
            addr=node.addr,
            mode='fastpath'  # we don't want to do any solving
        )

        # annotate the stack pointer
        concrete_state.regs.sp = concrete_state.regs.sp.annotate(StackLocationAnnotation(8))

        # give it enough stack space
        concrete_state.regs.bp = concrete_state.regs.sp + 0x100000

        return VariableRecoveryState(self, self.project.arch, [ concrete_state ])

    def _merge_states(self, *states):

        if len(states) == 1:
            return states[0]

        # FIXME: SimFastMemory doesn't support merge. We should do a merge here. Fix it later.
        return states[0]

        return reduce(lambda s_0, s_1: s_0.merge(s_1), states[1:], states[0])

    def _run_on_node(self, node, state):
        """


        :param angr.Block node:
        :param VariableRecoveryState state:
        :return:
        """

        state.concrete_states[0].registers.load(184, 8)

        concrete_state = state.get_concrete_state(node.addr)

        successors = self.project.factory.successors(concrete_state,
                                                     addr=node.addr,
                                                     size=node.size,
                                                     opt_level=0  # disable the optimization in order to have
                                                                  # instruction-level analysis results
                                                     )
        output_states = successors.all_successors

        state = state.copy()
        state.concrete_states = output_states

        self._node_to_state[node] = state

        return True, state

    def _intra_analysis(self):
        pass

    def _post_analysis(self):
        pass


register_analysis(VariableRecovery, 'VariableRecovery')
