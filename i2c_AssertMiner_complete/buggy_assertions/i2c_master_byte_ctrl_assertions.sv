module i2c_master_byte_ctrl_assertions (
    input logic       clk,
    input logic       rst,
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

  a_acknowledged_command_keeps_go_low: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    ((read || write || stop) && cmd_ack) |-> (go == 1'b0)
  );

  a_nonzero_counter_keeps_done_low: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    (dcnt != 3'h0) |-> (cnt_done == 1'b0)
  );

  a_go_has_pending_command_source: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    go |-> ((read || write || stop) && !cmd_ack)
  );

  a_done_has_zero_counter_source: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    cnt_done |-> (dcnt == 3'h0)
  );

  a_selected_command_generates_go: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    ((read || write || stop) && !cmd_ack && din[0]) |-> go
  );

  a_load_preserves_input_scope: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    ld |-> ##1 (din === din)
  );

  a_shift_load_observation: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    ld |-> aq_missing_shift_monitor.load_seen
  );

  a_counter_terminal_observation: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    cnt_done |-> aq_missing_counter_monitor.zero_seen
  );

  a_pending_command_keeps_go_low: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    ((read || write || stop) && !cmd_ack) |-> (go == 1'b0)
  );

  a_zero_counter_keeps_done_low: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    (dcnt == 3'h0) |-> (cnt_done == 1'b0)
  );

endmodule
