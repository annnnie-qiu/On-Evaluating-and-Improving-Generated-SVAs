module i2c_master_byte_ctrl_assertions (
    input logic       clk,
    input logic       nReset,
    input logic       read,
    input logic       write,
    input logic       stop,
    input logic       cmd_ack,
    input logic       go,
    input logic       ld,
    input logic [7:0] sr,
    input logic [7:0] din,
    input logic       shift,
    input logic       core_rxd,
    input logic [2:0] dcnt,
    input logic       cnt_done
);

  a_go_generation: assert property (@(posedge clk) disable iff (!nReset)
    ((read || write || stop) && !cmd_ack) |-> ##1 (go == 1'b1)
  );

  a_shift_register_load: assert property (@(posedge clk) disable iff (!nReset)
    ld |-> ##1 (sr == din)
  );

  a_shift_register_shift: assert property (@(posedge clk) disable iff (!nReset)
    shift |-> ##1 (sr == {sr[6:0], core_rxd})
  );

  a_counter_done: assert property (@(posedge clk) disable iff (!nReset)
    (dcnt == 3'h0) |-> ##1 (cnt_done == 1'b1)
  );

endmodule
