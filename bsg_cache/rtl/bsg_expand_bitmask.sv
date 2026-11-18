/**
 *  bsg_expand_bitmask.sv
 *
 *  This module expands each bit in the input vector by the factor of
 *  expand_p.
 *  
 *  @author anonymized
 * 
 *
 *  example
 *  ------------------------
 *  in_width_p=2, expand_p=4
 *  ------------------------
 *  i=00 -> o=0000_0000
 *  i=01 -> o=0000_1111
 *  i=10 -> o=1111_0000
 *  i=11 -> o=1111_1111
 *
 */

module bsg_expand_bitmask #(parameter in_width_p = 10
                           ,parameter expand_p = 10
                           ,localparam safe_expand_lp = (((expand_p)>(1)) ? (expand_p) : (1)))
(
  
    input clk_i,
input [in_width_p-1:0] i
  , output logic [(in_width_p*safe_expand_lp)-1:0] o
);

  always_comb
    for (integer k = 0; k < in_width_p; k++)
      o[safe_expand_lp*k+:safe_expand_lp] = {safe_expand_lp{i[k]}};

endmodule
