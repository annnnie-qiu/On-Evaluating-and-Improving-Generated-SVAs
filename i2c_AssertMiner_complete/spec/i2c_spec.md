# I2C Master Verification Specification

This specification is derived from the supplied verification-item descriptions.
It describes the intended properties used to evaluate the generated assertion set.

## Bit controller

- `clk_en` shall be asserted on the next cycle when the divider counter is zero,
  the core is disabled, or clock synchronization is detected.
- `slave_wait` shall be asserted when the controller releases SCL high while the
  sampled SCL input remains low.
- A falling SDA transition while SCL is high shall produce `sta_condition` on the
  next cycle.
- A rising SDA transition while SCL is high shall produce `sto_condition` on the
  next cycle.
- A detected start condition shall set `busy`; a detected stop condition shall
  clear `busy`.
- Arbitration loss shall be reported when SDA checking is active, the controller
  releases SDA high, and the sampled SDA value is low.
- `dout` shall capture the sampled SDA value following a detected rising SCL edge.

## Byte controller

- `go` shall be asserted for an outstanding read, write, or stop command while
  command acknowledgement is low.
- Loading the byte controller shall load `din` into the shift register on the next
  cycle.
- A shift request shall shift the register left and append `core_rxd` on the next
  cycle.
- `cnt_done` shall be asserted when the bit counter reaches zero.

## Top-level controller

- The WISHBONE interrupt output shall be asserted after both the interrupt flag
  and interrupt-enable signal are active.
