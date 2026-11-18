module f_permutation_assertions (
    input logic clk, input logic reset, input logic [575:0] in,
    input logic in_ready, input logic ack, input logic [1599:0] out,
    input logic out_ready, input logic [10:0] i,
    input logic [1599:0] round_in, input logic update,
    input logic accept, input logic calc
);
  a_raw_004: assert property (@(posedge clk) disable iff (reset) (in_ready && !calc) |-> ack == 1'b1);
  a_raw_005: assert property (@(posedge clk) disable iff (reset) accept |-> round_in == {in ^ out[1599:1024], out[1023:0]});
  a_raw_011: assert property (@(posedge clk) disable iff (reset) (in_ready && ~calc) |-> ack);
  a_raw_012: assert property (@(posedge clk) disable iff (reset) accept |-> (round_in == {in ^ out[1599:1024], out[1023:0]}));
  a_raw_017: assert property (@(posedge clk) disable iff (reset) (in_ready && !calc) |-> ack == 1'b1);
  a_raw_018: assert property (@(posedge clk) disable iff (reset) accept |-> round_in == {in ^ out[1599:1024], out[1023:0]});
  a_raw_022: assert property (@(posedge clk) disable iff (reset) i[10] |-> out_ready);
  a_raw_023: assert property (@(posedge clk) disable iff (reset) (accept || (calc && ~i[10])) |-> calc);
endmodule

