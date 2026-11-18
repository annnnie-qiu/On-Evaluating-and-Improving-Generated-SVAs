module bsg_round_robin_1_to_n_assertions #(parameter num_out_p = 8)
  (input clk_i
   , input reset_i
   , input valid_i
   , input ready_and_o
   , input [num_out_p-1:0] valid_o
   , input [num_out_p-1:0] ready_and_i
   );

  generate
    begin : common_interface_properties
      assert_valid_o_reduction_matches_valid_i:
        assert property (@(posedge clk_i) disable iff (reset_i)
          ((|valid_o) == valid_i));

      assert_valid_o_is_onehot0:
        assert property (@(posedge clk_i) disable iff (reset_i)
          $onehot0(valid_o));

      assert_valid_i_drives_onehot_valid_o:
        assert property (@(posedge clk_i) disable iff (reset_i)
          valid_i |-> $onehot(valid_o));

      assert_fire_equivalence:
        assert property (@(posedge clk_i) disable iff (reset_i)
          ((valid_i & ready_and_o) == (|(valid_o & ready_and_i))));

      assert_ready_matches_selected_when_valid:
        assert property (@(posedge clk_i) disable iff (reset_i)
          valid_i |-> (ready_and_o == (|(valid_o & ready_and_i))));

      cover_valid_input_seen:
        cover property (@(posedge clk_i) disable iff (reset_i)
          valid_i);

      cover_any_output_valid:
        cover property (@(posedge clk_i) disable iff (reset_i)
          (|valid_o));

      cover_any_handshake:
        cover property (@(posedge clk_i) disable iff (reset_i)
          (valid_i & ready_and_o));

      cover_valid_backpressured:
        cover property (@(posedge clk_i) disable iff (reset_i)
          (valid_i & ~ready_and_o));
    end
  endgenerate

  genvar gi;
  generate
    for (gi = 0; gi < num_out_p; gi = gi + 1) begin : per_output_properties
      assert_valid_bit_implies_valid_i:
        assert property (@(posedge clk_i) disable iff (reset_i)
          valid_o[gi] |-> valid_i);

      assert_selected_output_ready_mux:
        assert property (@(posedge clk_i) disable iff (reset_i)
          valid_o[gi] |-> (ready_and_o == ready_and_i[gi]));

      cover_output_valid:
        cover property (@(posedge clk_i) disable iff (reset_i)
          valid_o[gi]);

      cover_output_handshake:
        cover property (@(posedge clk_i) disable iff (reset_i)
          (valid_o[gi] & ready_and_i[gi]));

      cover_output_backpressured:
        cover property (@(posedge clk_i) disable iff (reset_i)
          (valid_o[gi] & ~ready_and_i[gi]));
    end
  endgenerate

  generate
    if (num_out_p == 1) begin : one_to_one_properties
      assert_one_to_one_valid_passthrough:
        assert property (@(posedge clk_i) disable iff (reset_i)
          (valid_o[0] == valid_i));

      assert_one_to_one_ready_passthrough:
        assert property (@(posedge clk_i) disable iff (reset_i)
          (ready_and_o == ready_and_i[0]));

      cover_one_to_one_transfer:
        cover property (@(posedge clk_i) disable iff (reset_i)
          (valid_o[0] & ready_and_i[0]));
    end
    else begin : one_to_n_properties
      genvar gj;
      for (gj = 0; gj < num_out_p; gj = gj + 1) begin : per_output_round_robin_properties
        localparam int next_idx_lp = (gj == (num_out_p-1)) ? 0 : (gj + 1);

        assert_no_advance_without_selected_ready:
          assert property (@(posedge clk_i) disable iff (reset_i)
            ($past(!reset_i) && $past(valid_o[gj] && !ready_and_i[gj]) && valid_i)
            |-> valid_o[gj]);

        assert_advance_after_selected_handshake:
          assert property (@(posedge clk_i) disable iff (reset_i)
            ($past(!reset_i) && $past(valid_o[gj] && ready_and_i[gj]) && valid_i)
            |-> valid_o[next_idx_lp]);

        assert_ready_mux_stays_without_advance:
          assert property (@(posedge clk_i) disable iff (reset_i)
            ($past(!reset_i) && $past(valid_o[gj] && !ready_and_i[gj]))
            |-> (ready_and_o == ready_and_i[gj]));

        assert_ready_mux_advances_after_handshake:
          assert property (@(posedge clk_i) disable iff (reset_i)
            ($past(!reset_i) && $past(valid_o[gj] && ready_and_i[gj]))
            |-> (ready_and_o == ready_and_i[next_idx_lp]));

        cover_round_robin_no_advance:
          cover property (@(posedge clk_i) disable iff (reset_i)
            ($past(!reset_i) && $past(valid_o[gj] && !ready_and_i[gj]) && valid_i && valid_o[gj]));

        cover_round_robin_advance:
          cover property (@(posedge clk_i) disable iff (reset_i)
            ($past(!reset_i) && $past(valid_o[gj] && ready_and_i[gj]) && valid_i && valid_o[next_idx_lp]));
      end

      cover_one_to_n_wraparound:
        cover property (@(posedge clk_i) disable iff (reset_i)
          ($past(!reset_i) && $past(valid_o[num_out_p-1] && ready_and_i[num_out_p-1]) && valid_i && valid_o[0]));
    end
  endgenerate

endmodule

bind bsg_round_robin_1_to_n bsg_round_robin_1_to_n_assertions #(.num_out_p(num_out_p)) i_assertions (.*);