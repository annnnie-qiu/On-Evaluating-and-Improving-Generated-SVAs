module padder_assertions (
    input logic clk, input logic reset, input logic [8:0] i,
    input logic in_ready, input logic is_last, input logic buffer_full,
    input logic [575:0] out, input logic out_ready, input logic f_ack,
    input logic state, input logic done, input logic [63:0] v1,
    input logic accept, input logic update
);
  logic history_valid;
  always_ff @(posedge clk) begin
    if (reset) history_valid <= 1'b0;
    else history_valid <= 1'b1;
  end

  a_raw_001: assert property (@(posedge clk) disable iff (reset) i[8] |-> buffer_full == 1'b1);
  a_raw_002: assert property (@(posedge clk) disable iff (reset) buffer_full |-> out_ready == 1'b1);
  a_raw_003: assert property (@(posedge clk) disable iff (reset) (state == 1'b0 && in_ready && !buffer_full) |-> accept == 1'b1);
  a_raw_007: assert property (@(posedge clk) disable iff (reset) i[8] |-> buffer_full);
  a_raw_008: assert property (@(posedge clk) disable iff (reset) buffer_full |-> out_ready);
  a_raw_009: assert property (@(posedge clk) disable iff (reset) (state == 1'b0 && in_ready && ~buffer_full) |-> accept);
  a_raw_010: assert property (@(posedge clk) disable iff (reset) (accept || (state && ~buffer_full && ~done)) |-> update);
  a_raw_013: assert property (@(posedge clk) disable iff (reset) i[8] |-> buffer_full == 1'b1);
  a_raw_014: assert property (@(posedge clk) disable iff (reset) buffer_full |-> out_ready == 1'b1);
  a_raw_015: assert property (@(posedge clk) disable iff (reset) (state == 1'b0 && in_ready && !buffer_full) |-> accept == 1'b1);
  a_raw_016: assert property (@(posedge clk) disable iff (reset) (accept || (state && !buffer_full && !done)) |-> update == 1'b1);
  a_raw_019: assert property (@(posedge clk) disable iff (reset) update |-> (out == {out[575-64:0], v1}));
  a_raw_020: assert property (@(posedge clk) disable iff (reset) is_last |-> (state == 1'b1));
  a_raw_021: assert property (@(posedge clk) disable iff (reset) (state && out_ready) |-> done);
  a_raw_024: assert property (@(posedge clk) disable iff (reset) update |-> out == {out[575-64:0], v1});
  a_raw_025: assert property (@(posedge clk) disable iff (reset) is_last |-> state == 1'b1);

  a_behavior_001: assert property (@(posedge clk) disable iff (reset) (!buffer_full) |-> (!out_ready));
  a_behavior_002: assert property (@(posedge clk) disable iff (reset) (history_valid && state && !$past(state)) |-> $past(is_last));
  a_behavior_003: assert property (@(posedge clk) disable iff (reset) (buffer_full && !done) |-> out_ready);
  a_behavior_004: assert property (@(posedge clk) disable iff (reset) $past(reset) |-> (buffer_full || !buffer_full));
  a_behavior_005: assert property (@(posedge clk) disable iff (reset) buffer_full |-> i[8]);

  a_check_001: assert property (@(posedge clk) disable iff (reset) i[8] |-> !buffer_full);
  a_check_002: assert property (@(posedge clk) disable iff (reset) buffer_full |-> !out_ready);
  a_check_003: assert property (@(posedge clk) disable iff (reset) accept |-> !update);
  a_check_006: assert property (@(posedge clk) disable iff (reset) (buffer_full && !i[8]) |-> out_ready);
  a_check_007: assert property (@(posedge clk) disable iff (reset) (out_ready && !buffer_full) |-> done);
  a_check_008: assert property (@(posedge clk) disable iff (reset) (accept && state) |-> update);
endmodule
