# SHA3 Hardware Verification Specification

This fixed evaluation specification is reconstructed from the comments in the
supplied SHA3 RTL and the behavior targeted by the supplied AssertLLM
assertions. The source ZIP did not include a separate natural-language
specification. This file is shared without modification by the normal and
controlled assertion sets.

## Padder

- When the most-significant bit of the padder occupancy counter is set, the
  padder shall report `buffer_full`.
- When the buffer is full, the padder shall report `out_ready`.
- The padder shall accept input when it is not in the final-input state, input
  is ready, and the buffer is not full.
- An accepted input, or an active final-input state with available buffer
  capacity, shall enable a padder update while processing is not done.
- A padder update shall shift the output buffer and append the computed input
  word on the following state update.
- A final input indication shall move the padder to its final-input state.
- A final-input state with a ready output shall complete padding.

## F permutation

- The permutation block shall acknowledge an input that is ready while no
  permutation calculation is active.
- On an accepted input, the round input shall combine the input block with the
  upper portion of the current state and retain the lower state portion.
- Reaching the final round shall make the permutation output ready on the
  following state update.
- An accepted input shall start calculation; an active calculation shall remain
  active until the final round.

## Scope

- The requirements above define the behaviors evaluated in this project.
- Internal RTL details not stated above are not specification failures by
  themselves.
