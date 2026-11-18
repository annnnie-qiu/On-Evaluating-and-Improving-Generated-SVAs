module bsg_flow_counter_assertions #(
    parameter els_p = 8,
    parameter count_free_p = 0,
    parameter ready_THEN_valid_p = 0,
    parameter ptr_width_lp = ($clog2({1'b0, els_p}+1))
  )
  (
    input                     clk_i,
    input                     reset_i,
    input                     v_i,
    input                     ready_param_i,
    input                     yumi_i,
    input [ptr_width_lp-1:0]  count_o
  );

  localparam logic [ptr_width_lp:0] els_wide_lp = els_p;
  localparam logic [ptr_width_lp:0] one_wide_lp = {{ptr_width_lp{1'b0}}, 1'b1};

  wire enque_expected = (ready_THEN_valid_p != 0) ? v_i : (v_i & ready_param_i);
  wire [ptr_width_lp:0] count_wide = {1'b0, count_o};
  wire [ptr_width_lp:0] enque_wide = {{ptr_width_lp{1'b0}}, enque_expected};
  wire [ptr_width_lp:0] yumi_wide = {{ptr_width_lp{1'b0}}, yumi_i};

  generate
    if (count_free_p) begin : gen_free_count_environment
      assume_free_count_no_underflow_current: assume property (@(posedge clk_i) disable iff (reset_i)
        ((count_wide + yumi_wide) >= enque_wide));

      assume_free_count_no_overflow_current: assume property (@(posedge clk_i) disable iff (reset_i)
        ((count_wide + yumi_wide - enque_wide) <= els_wide_lp));
    end else begin : gen_used_count_environment
      assume_used_count_no_underflow_current: assume property (@(posedge clk_i) disable iff (reset_i)
        ((count_wide + enque_wide) >= yumi_wide));

      assume_used_count_no_overflow_current: assume property (@(posedge clk_i) disable iff (reset_i)
        ((count_wide + enque_wide - yumi_wide) <= els_wide_lp));
    end
  endgenerate

  generate
    if (count_free_p) begin : gen_free_count_assertions
      assert_free_count_exact_update: assert property (@(posedge clk_i) disable iff (reset_i)
        $past(!reset_i) |->
          (count_wide == ({1'b0, $past(count_o)} + $past(yumi_wide) - $past(enque_wide))));

      assert_free_count_increments_on_yumi_only: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(yumi_i) && !$past(enque_expected)) |->
          (count_wide == ({1'b0, $past(count_o)} + one_wide_lp)));

      assert_free_count_decrements_on_enque_only: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(enque_expected) && !$past(yumi_i)) |->
          (count_wide == ({1'b0, $past(count_o)} - one_wide_lp)));

      assert_free_count_stable_on_balanced_transfer: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && ($past(enque_expected) == $past(yumi_i))) |->
          (count_o == $past(count_o)));

      cover_free_count_enqueue_without_yumi: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(enque_expected) && !$past(yumi_i)));

      cover_free_count_yumi_without_enqueue: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && !$past(enque_expected) && $past(yumi_i)));

      cover_free_count_balanced_transfer: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(enque_expected) && $past(yumi_i)));

      cover_free_count_idle_transfer: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && !$past(enque_expected) && !$past(yumi_i)));

      cover_free_count_reaches_empty: cover property (@(posedge clk_i) disable iff (reset_i)
        (count_wide == { (ptr_width_lp+1) {1'b0} }));

      cover_free_count_reaches_full: cover property (@(posedge clk_i) disable iff (reset_i)
        (count_wide == els_wide_lp));
    end else begin : gen_used_count_assertions
      assert_used_count_exact_update: assert property (@(posedge clk_i) disable iff (reset_i)
        $past(!reset_i) |->
          (count_wide == ({1'b0, $past(count_o)} + $past(enque_wide) - $past(yumi_wide))));

      assert_used_count_increments_on_enque_only: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(enque_expected) && !$past(yumi_i)) |->
          (count_wide == ({1'b0, $past(count_o)} + one_wide_lp)));

      assert_used_count_decrements_on_yumi_only: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && !$past(enque_expected) && $past(yumi_i)) |->
          (count_wide == ({1'b0, $past(count_o)} - one_wide_lp)));

      assert_used_count_stable_on_balanced_transfer: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && ($past(enque_expected) == $past(yumi_i))) |->
          (count_o == $past(count_o)));

      cover_used_count_enqueue_without_yumi: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(enque_expected) && !$past(yumi_i)));

      cover_used_count_yumi_without_enqueue: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && !$past(enque_expected) && $past(yumi_i)));

      cover_used_count_balanced_transfer: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(enque_expected) && $past(yumi_i)));

      cover_used_count_idle_transfer: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && !$past(enque_expected) && !$past(yumi_i)));

      cover_used_count_reaches_empty: cover property (@(posedge clk_i) disable iff (reset_i)
        (count_wide == { (ptr_width_lp+1) {1'b0} }));

      cover_used_count_reaches_full: cover property (@(posedge clk_i) disable iff (reset_i)
        (count_wide == els_wide_lp));
    end
  endgenerate

  generate
    if (ready_THEN_valid_p) begin : gen_ready_then_valid_protocol_assertions
      if (count_free_p) begin : gen_ready_then_free_count
        assert_ready_then_free_ready_low_still_enqueues: assert property (@(posedge clk_i) disable iff (reset_i)
          ($past(!reset_i) && $past(v_i) && !$past(ready_param_i) && !$past(yumi_i)) |->
            (count_wide == ({1'b0, $past(count_o)} - one_wide_lp)));

        cover_ready_then_free_ready_low_enqueue: cover property (@(posedge clk_i) disable iff (reset_i)
          ($past(!reset_i) && $past(v_i) && !$past(ready_param_i) && !$past(yumi_i)));
      end else begin : gen_ready_then_used_count
        assert_ready_then_used_ready_low_still_enqueues: assert property (@(posedge clk_i) disable iff (reset_i)
          ($past(!reset_i) && $past(v_i) && !$past(ready_param_i) && !$past(yumi_i)) |->
            (count_wide == ({1'b0, $past(count_o)} + one_wide_lp)));

        cover_ready_then_used_ready_low_enqueue: cover property (@(posedge clk_i) disable iff (reset_i)
          ($past(!reset_i) && $past(v_i) && !$past(ready_param_i) && !$past(yumi_i)));
      end
    end else begin : gen_ready_and_valid_protocol_assertions
      assert_ready_and_valid_ready_low_blocks_enqueue: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(v_i) && !$past(ready_param_i) && !$past(yumi_i)) |->
          (count_o == $past(count_o)));

      cover_ready_and_valid_ready_low_no_enqueue: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(v_i) && !$past(ready_param_i) && !$past(yumi_i)));

      cover_ready_and_valid_ready_high_enqueue: cover property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(v_i) && $past(ready_param_i) && !$past(yumi_i)));
    end
  endgenerate

endmodule

bind bsg_flow_counter bsg_flow_counter_assertions #(.els_p(els_p), .count_free_p(count_free_p), .ready_THEN_valid_p(ready_THEN_valid_p), .ptr_width_lp(ptr_width_lp)) i_assertions (.*);