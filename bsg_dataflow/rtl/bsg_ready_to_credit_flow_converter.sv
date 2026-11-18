// This module converts between the valid-ready (input) and 
// valid-credit (output) handshakes, by keeping the count of 
// available credits

module bsg_ready_to_credit_flow_converter #( parameter credit_initial_p = 8
                                           , parameter credit_max_val_p = 8
                                           , parameter decimation_p     =  1

                                           //local parameter 
                                           , parameter ptr_width_lp = 
                                               ($clog2({1'b0, credit_max_val_p}+1))
                                           )                           

    ( input  clk_i
    , input  reset_i

    , input  v_i
    , output ready_and_o

    , output v_o
    , input  credit_i

    );

// if toekens are used, up and down values would not be single bits anymore
localparam step_width_lp = ($clog2({1'b0, decimation_p}+1));

logic      [step_width_lp-1:0] up,down;

// credit_counter signal
logic [ptr_width_lp-1:0] credit_cnt;

// conversion between valid-credit and valid-credit protocols
// in case of reset, credit_cnt is not zero, so the ready
// and valid signals
assign ready_and_o = (credit_cnt!=0);
assign v_o     = v_i & ready_and_o;
assign up      = credit_i ? step_width_lp'($unsigned(decimation_p)) : step_width_lp'(0);
assign down    = {{(step_width_lp-1){1'b0}},v_o};

// counter for credits. When each data is sent it goes down
// by 1 and when it receives a credit acknowledge it goes 
// up. If other side of handshake has more buffer it can
// send some credit acknowledges at first to raise the limit
bsg_counter_up_down_variable #( .max_val_p(credit_max_val_p)  
                              , .init_val_p(credit_initial_p) 
                              , .max_step_p(decimation_p)
                              ) credit_counter

    ( .clk_i(clk_i)
    , .reset_i(reset_i)

    , .up_i(up)
    , .down_i(down)

    , .count_o(credit_cnt)
    );

endmodule 
