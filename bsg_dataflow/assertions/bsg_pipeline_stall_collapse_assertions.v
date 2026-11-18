module bsg_pipeline_stall_collapse_assertions #(
  parameter int stages_p = 8,
  parameter logic [stages_p-1:0] skip_p = '0,
  parameter logic debug_p = 1'b0
) (
  input clk_i,
  input reset_i,
  input valid_i,
  input global_stall_i,
  input ready_and_o,
  input ready_and_i,
  input [stages_p-1:0] en_o,
  input [stages_p-1:0] valid_o
);

  localparam logic [stages_p-1:0] registered_mask_lp = ~skip_p;

  logic [stages_p-1:0] scan_expected;
  logic [stages_p-1:0] ready_adj_expected;
  logic [stages_p-1:0] v_li_expected;

  genvar stage;
  generate
    for (stage = 0; stage < stages_p; stage = stage + 1) begin : gen_expected_vectors
      if (stage == 0) begin : gen_scan_first
        assign scan_expected[stage] = valid_o[stage];
      end else begin : gen_scan_rest
        assign scan_expected[stage] = scan_expected[stage-1] & valid_o[stage];
      end

      if (stage == stages_p-1) begin : gen_vli_input
        assign v_li_expected[stage] = valid_i;
      end else begin : gen_vli_shift
        assign v_li_expected[stage] = valid_o[stage+1];
      end

      assign ready_adj_expected[stage] = (ready_and_i | ~scan_expected[stage]) & ~global_stall_i;
    end
  endgenerate

  always_comb begin : comb_ready_checks
    assert_ready_and_o_expected: assert (ready_and_o == ready_adj_expected[stages_p-1]);
    assert_ready_and_o_all_valid_formula: assert (ready_and_o == ((ready_and_i | ~(&valid_o)) & ~global_stall_i));
    assert_global_stall_blocks_ready_and_o: assert (!global_stall_i || !ready_and_o);
    assert_downstream_ready_passes_ready_and_o: assert (!(ready_and_i && !global_stall_i) || ready_and_o);
    assert_full_pipeline_without_downstream_not_ready: assert (!((!ready_and_i) && (&valid_o)) || !ready_and_o);

    cover_ready_and_o_high: cover (!reset_i && ready_and_o);
    cover_ready_and_o_low: cover (!reset_i && !ready_and_o);
    cover_input_valid_accepted: cover (!reset_i && ready_and_o && valid_i);
    cover_global_stall_blocks_ready: cover (!reset_i && global_stall_i && !ready_and_o);
    cover_valid_outputs_all_zero: cover (!reset_i && (valid_o == {stages_p{1'b0}}));
    cover_valid_outputs_all_one: cover (!reset_i && (&valid_o));
  end

  generate
    if (registered_mask_lp != {stages_p{1'b0}}) begin : gen_registered_checks
      always_comb begin : comb_registered_checks
        assert_registered_en_expected: assert ((((en_o ^ (v_li_expected & ready_adj_expected)) & registered_mask_lp) == {stages_p{1'b0}}));
        assert_registered_en_blocked_by_stall: assert (!global_stall_i || ((en_o & registered_mask_lp) == {stages_p{1'b0}}));
        assert_registered_en_when_downstream_ready: assert (!(ready_and_i && !global_stall_i) || (((en_o ^ v_li_expected) & registered_mask_lp) == {stages_p{1'b0}}));

        cover_registered_en_any: cover (!reset_i && (|(en_o & registered_mask_lp)));
        cover_registered_en_none: cover (!reset_i && ((en_o & registered_mask_lp) == {stages_p{1'b0}}));
      end

      assert_registered_update_valids: assert property (@(posedge clk_i) disable iff (reset_i)
        $past(!reset_i) |-> ((((valid_o ^ $past(v_li_expected)) & $past(ready_adj_expected) & registered_mask_lp) == {stages_p{1'b0}}))
      );

      assert_registered_hold_valids: assert property (@(posedge clk_i) disable iff (reset_i)
        $past(!reset_i) |-> ((((valid_o ^ $past(valid_o)) & (~$past(ready_adj_expected)) & registered_mask_lp) == {stages_p{1'b0}}))
      );

      cover_registered_update_event: cover property (@(posedge clk_i) disable iff (reset_i)
        $past(!reset_i) && (|(ready_adj_expected & registered_mask_lp))
      );

      cover_registered_hold_event: cover property (@(posedge clk_i) disable iff (reset_i)
        $past(!reset_i) && (|((~ready_adj_expected) & registered_mask_lp))
      );

      cover_registered_valid_present: cover property (@(posedge clk_i) disable iff (reset_i)
        $past(!reset_i) && (|(valid_o & registered_mask_lp))
      );
    end
  endgenerate

  generate
    if (skip_p != {stages_p{1'b0}}) begin : gen_skipped_checks
      always_comb begin : comb_skipped_checks
        assert_skipped_valid_expected: assert ((((valid_o ^ v_li_expected) & skip_p) == {stages_p{1'b0}}));

        cover_skipped_valid_any: cover (!reset_i && (|(valid_o & skip_p)));
        cover_skipped_valid_none: cover (!reset_i && ((valid_o & skip_p) == {stages_p{1'b0}}));
      end
    end
  endgenerate

endmodule

bind bsg_pipeline_stall_collapse bsg_pipeline_stall_collapse_assertions #(.stages_p(stages_p), .skip_p(skip_p), .debug_p(debug_p)) i_assertions (.*);