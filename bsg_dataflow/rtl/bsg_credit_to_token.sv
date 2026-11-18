// This module is a counter for credits, that every decimation_p
// credits it would assert token_o signal once. 
// It also supports a ready_i signal which declares when it can
// assert token_o. For normal use it could be set to one.

module bsg_credit_to_token #( parameter decimation_p = 8
                            , parameter max_val_p = 8
                            )
       ( input clk_i
       , input reset_i

       , input credit_i
       , input ready_i

       , output token_o
       );

localparam counter_width_lp = ($clog2({1'b0, max_val_p}+1));
localparam step_width_lp    = ($clog2({1'b0, decimation_p}+1));

logic [counter_width_lp-1:0]  count;
logic [step_width_lp-1:0]     up,down;
logic                         token_ready, token_almost_ready;

bsg_counter_up_down_variable #(.max_val_p(max_val_p)
                              ,.init_val_p(0)
                              ,.max_step_p(decimation_p)
                              ) credit_counter
    ( .clk_i(clk_i)
    , .reset_i(reset_i)

    , .up_i(up)
    , .down_i(down)

    , .count_o(count)
    );

// counting the number of credits, and each token would decrease the count
// by deciation_p.
assign up   = {{(step_width_lp-1){1'b0}},credit_i};
assign down = token_o ? step_width_lp'($unsigned(decimation_p)) : step_width_lp'(0);

// if count is one less than decimation_p but credit_i is also asserted and
// ready signal is high, we don't need to wait for next time ready_i signal
// is asserted and we can send a token. In this condition count would be set
// to zero using down and up signal.
assign token_ready        = (count >= decimation_p);
assign token_almost_ready = (count >= $unsigned(decimation_p-1));
assign token_o = ready_i & (token_ready | (token_almost_ready & credit_i));

endmodule
