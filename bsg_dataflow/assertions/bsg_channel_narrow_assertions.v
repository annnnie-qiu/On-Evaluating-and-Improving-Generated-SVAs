module bsg_channel_narrow_assertions #(
    parameter width_in_p = 8,
    parameter width_out_p = 8,
    parameter lsb_to_msb_p = 1
)(
    input                          clk_i,
    input                          reset_i,
    input  [width_in_p-1:0]        data_i,
    input                          deque_o,
    input  [width_out_p-1:0]       data_o,
    input                          deque_i
);

  localparam int divisions_lp = (width_in_p % width_out_p == 0)
                              ? (width_in_p / width_out_p)
                              : ((width_in_p / width_out_p) + 1);

  localparam int count_width_lp = (((divisions_lp) == 1) || ((divisions_lp) == 0))
                                ? 1
                                : $clog2(divisions_lp);

  function automatic [width_out_p-1:0] expected_data_chunk(
      input [width_in_p-1:0] din,
      input [count_width_lp-1:0] idx
  );
    integer bit_idx;
    integer source_idx;
    begin
      expected_data_chunk = '0;
      for (bit_idx = 0; bit_idx < width_out_p; bit_idx = bit_idx + 1) begin
        if (divisions_lp == 1) begin
          source_idx = bit_idx;
        end else if (lsb_to_msb_p != 0) begin
          source_idx = (idx * width_out_p) + bit_idx;
        end else begin
          source_idx = ((divisions_lp - 1 - idx) * width_out_p) + bit_idx;
        end

        if ((source_idx >= 0) && (source_idx < width_in_p)) begin
          expected_data_chunk[bit_idx] = din[source_idx];
        end else begin
          expected_data_chunk[bit_idx] = 1'b0;
        end
      end
    end
  endfunction

  generate
    if (divisions_lp == 1) begin : gen_single_division_assertions
      always_comb begin : single_division_comb_checks
        a_single_data_o_matches_padded_input: assert (data_o == expected_data_chunk(data_i, {count_width_lp{1'b0}}));
        a_single_deque_o_forwards_deque_i: assert (deque_o == deque_i);
        a_single_deque_o_low_when_deque_i_low: assert ((!deque_i) -> (!deque_o));
        c_single_deque_o_high: cover (deque_o);
        c_single_data_o_nonzero: cover (|data_o);
      end
    end else begin : gen_multi_division_assertions
      begin : auxiliary_rtl_logic
        logic [count_width_lp-1:0] count_shadow_r;
        logic [count_width_lp-1:0] count_shadow_n;

        always_comb begin : count_shadow_next_logic
          count_shadow_n = count_shadow_r;
          if (deque_i) begin
            if (count_shadow_r == count_width_lp'(divisions_lp - 1)) begin
              count_shadow_n = '0;
            end else begin
              count_shadow_n = count_shadow_r + {{(count_width_lp-1){1'b0}}, 1'b1};
            end
          end
        end

        always_ff @(posedge clk_i) begin : count_shadow_register
          if (reset_i) begin
            count_shadow_r <= '0;
          end else begin
            count_shadow_r <= count_shadow_n;
          end
        end
      end

      begin : multi_division_output_assertions
        a_multi_data_o_matches_selected_piece: assert property (@(posedge clk_i) disable iff (reset_i)
          data_o == expected_data_chunk(data_i, auxiliary_rtl_logic.count_shadow_r)
        );

        a_multi_deque_o_matches_last_piece_deque: assert property (@(posedge clk_i) disable iff (reset_i)
          deque_o == (deque_i && (auxiliary_rtl_logic.count_shadow_r == count_width_lp'(divisions_lp - 1)))
        );

        a_multi_deque_o_low_when_deque_i_low: assert property (@(posedge clk_i) disable iff (reset_i)
          (!deque_i) |-> (!deque_o)
        );

        a_multi_deque_o_implies_last_piece: assert property (@(posedge clk_i) disable iff (reset_i)
          deque_o |-> (deque_i && (auxiliary_rtl_logic.count_shadow_r == count_width_lp'(divisions_lp - 1)))
        );

        a_multi_nonlast_piece_blocks_deque_o: assert property (@(posedge clk_i) disable iff (reset_i)
          (auxiliary_rtl_logic.count_shadow_r != count_width_lp'(divisions_lp - 1)) |-> (!deque_o)
        );

        a_multi_last_piece_deque_o_follows_deque_i: assert property (@(posedge clk_i) disable iff (reset_i)
          (auxiliary_rtl_logic.count_shadow_r == count_width_lp'(divisions_lp - 1)) |-> (deque_o == deque_i)
        );

        a_multi_output_stable_without_deque_when_input_stable: assert property (@(posedge clk_i) disable iff (reset_i)
          ($past(!reset_i) && !$past(deque_i) && !deque_i && $stable(data_i)) |-> $stable(data_o)
        );

        c_multi_first_piece_consumed: cover property (@(posedge clk_i) disable iff (reset_i)
          (auxiliary_rtl_logic.count_shadow_r == '0) && deque_i
        );

        c_multi_last_piece_dequeued: cover property (@(posedge clk_i) disable iff (reset_i)
          deque_o
        );

        c_multi_data_o_nonzero: cover property (@(posedge clk_i) disable iff (reset_i)
          |data_o
        );

        c_multi_nonlast_piece_observed: cover property (@(posedge clk_i) disable iff (reset_i)
          auxiliary_rtl_logic.count_shadow_r != count_width_lp'(divisions_lp - 1)
        );
      end
    end
  endgenerate

endmodule

bind bsg_channel_narrow bsg_channel_narrow_assertions #(.width_in_p(width_in_p), .width_out_p(width_out_p), .lsb_to_msb_p(lsb_to_msb_p)) i_assertions (.*);