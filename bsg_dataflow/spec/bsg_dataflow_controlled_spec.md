# BSG Dataflow Controlled Specification

The supplied archive does not include a natural-language specification. This
controlled specification is reconstructed from the supplied RTL comments and
interface behavior. It must not be presented as an original benchmark spec.

## Serial-in/parallel-out FIFO requirements

- `ready_and_o` shall be high whenever the FIFO has space and low when all
  `els_p` entries are occupied.
- An accepted `valid_i` word shall be appended after the stored entries.
- When the accepted word is visible, its `valid_o` bit shall be high and its
  `data_o` entry shall equal `data_i`.
- With no stored word and no valid input, every `valid_o` bit shall be low.
- `yumi_cnt_i` entries shall be removed from the head on the following state
  update and shall not exceed the number of visible valid entries.
- Synchronous active-high `reset_i` shall clear the stored count and valid
  state. After reset, the FIFO shall have space and no valid output.

Replacing a required value with a tautology, narrowing the trigger, reversing
causality, changing reset semantics, or checking only part of the specified
interface is a specification-faithfulness failure.
