module f_permutation_assertions (
    input logic clk, input logic reset, input logic [575:0] in,
    input logic in_ready, input logic ack, input logic [1599:0] out,
    input logic out_ready, input logic [10:0] i,
    input logic [1599:0] round_in, input logic update,
    input logic accept, input logic calc
);
  logic history_valid;
  always_ff @(posedge clk) begin
    if (reset) history_valid <= 1'b0;
    else history_valid <= 1'b1;
  end

  a_raw_004: assert property (@(posedge clk) disable iff (reset) (in_ready && !calc) |-> ack == 1'b1);
  a_raw_005: assert property (@(posedge clk) disable iff (reset) accept |-> round_in == {in ^ out[1599:1024], out[1023:0]});
  a_raw_011: assert property (@(posedge clk) disable iff (reset) (in_ready && ~calc) |-> ack);
  a_raw_012: assert property (@(posedge clk) disable iff (reset) accept |-> (round_in == {in ^ out[1599:1024], out[1023:0]}));
  a_raw_017: assert property (@(posedge clk) disable iff (reset) (in_ready && !calc) |-> ack == 1'b1);
  a_raw_018: assert property (@(posedge clk) disable iff (reset) accept |-> round_in == {in ^ out[1599:1024], out[1023:0]});
  a_raw_022: assert property (@(posedge clk) disable iff (reset) i[10] |-> out_ready);
  a_raw_023: assert property (@(posedge clk) disable iff (reset) (accept || (calc && ~i[10])) |-> calc);

  a_behavior_006: assert property (@(posedge clk) disable iff (reset) (!in_ready && !calc) |-> !ack);
  a_behavior_007: assert property (@(posedge clk) disable iff (reset) (history_valid && out_ready && !$past(out_ready)) |-> $past(i[10]));
  a_behavior_008: assert property (@(posedge clk) disable iff (reset) (in_ready && !calc && !out_ready) |-> ack);
  a_behavior_009: assert property (@(posedge clk) disable iff (reset) $past(reset) |-> (ack || !ack));
  a_behavior_010: assert property (@(posedge clk) disable iff (reset) (in_ready && !calc) |-> accept);

  a_check_004: assert property (@(posedge clk) disable iff (reset) (in_ready && !calc) |-> !ack);
  a_check_005: assert property (@(posedge clk) disable iff (reset) accept |-> calc);
  a_check_009: assert property (@(posedge clk) disable iff (reset) (ack && calc) |-> out_ready);
  a_check_010: assert property (@(posedge clk) disable iff (reset) (accept && !ack) |-> update);
endmodule
