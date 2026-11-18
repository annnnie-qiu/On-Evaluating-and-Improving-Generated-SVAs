module bsg_fifo_tracker_assertions #(
    parameter els_p = 8,
    parameter ptr_width_lp = ((((els_p)==1) || ((els_p)==0)) ? 1 : $clog2((els_p)))
  )
  (
    input clk_i,
    input reset_i,
    input enq_i,
    input deq_i,
    input [ptr_width_lp-1:0] wptr_r_o,
    input [ptr_width_lp-1:0] rptr_r_o,
    input [ptr_width_lp-1:0] rptr_n_o,
    input full_o,
    input empty_o
  );

  localparam count_width_lp = ((els_p <= 1) ? 1 : $clog2(els_p+1));

  function automatic [ptr_width_lp-1:0] next_ptr(input [ptr_width_lp-1:0] ptr);
    begin
      if (els_p <= 1)
        next_ptr = '0;
      else if (ptr == (els_p-1))
        next_ptr = '0;
      else
        next_ptr = ptr + 1'b1;
    end
  endfunction

  // auxiliary_rtl_logic
  logic past_valid_r = 1'b0;
  logic model_known_r = 1'b0;
  logic model_enq_r = 1'b0;
  logic model_deq_r = 1'b1;

  always_ff @(posedge clk_i) begin
    past_valid_r <= !reset_i;

    if (reset_i) begin
      model_known_r <= 1'b1;
      model_enq_r <= 1'b0;
      model_deq_r <= 1'b1;
    end
    else if (model_known_r == 1'b1) begin
      if (enq_i | deq_i) begin
        model_enq_r <= enq_i;
        model_deq_r <= deq_i;
      end
    end
  end

  generate
    begin : protocol_assumptions
      assume_no_enq_when_full: assume property (@(posedge clk_i) disable iff (reset_i)
        (full_o |-> !enq_i));

      assume_no_deq_when_empty: assume property (@(posedge clk_i) disable iff (reset_i)
        (empty_o |-> !deq_i));
    end
  endgenerate

  generate
    begin : status_decode_properties
      assert_full_implies_equal_ptrs: assert property (@(posedge clk_i) disable iff (reset_i)
        (full_o |-> (wptr_r_o == rptr_r_o)));

      assert_empty_implies_equal_ptrs: assert property (@(posedge clk_i) disable iff (reset_i)
        (empty_o |-> (wptr_r_o == rptr_r_o)));

      assert_unequal_ptrs_not_full_or_empty: assert property (@(posedge clk_i) disable iff (reset_i)
        ((wptr_r_o != rptr_r_o) |-> (!full_o && !empty_o)));

      assert_full_matches_tracked_last_enq: assert property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) |-> (full_o == ((wptr_r_o == rptr_r_o) && model_enq_r))));

      assert_empty_matches_tracked_last_deq: assert property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) |-> (empty_o == ((wptr_r_o == rptr_r_o) && model_deq_r))));
    end
  endgenerate

  generate
    begin : pointer_integration_properties
      assert_rptr_n_matches_deq_input: assert property (@(posedge clk_i) disable iff (reset_i)
        (((model_known_r == 1'b1) && deq_i) |-> (rptr_n_o == next_ptr(rptr_r_o))));

      assert_rptr_n_holds_without_deq_input: assert property (@(posedge clk_i) disable iff (reset_i)
        (((model_known_r == 1'b1) && !deq_i) |-> (rptr_n_o == rptr_r_o)));

      assert_wptr_advances_after_enq: assert property (@(posedge clk_i) disable iff (reset_i)
        (((model_known_r == 1'b1) && (past_valid_r == 1'b1) && ($past(reset_i) == 1'b0) && $past(enq_i)) |->
          (wptr_r_o == next_ptr($past(wptr_r_o)))));

      assert_wptr_stable_without_enq: assert property (@(posedge clk_i) disable iff (reset_i)
        (((model_known_r == 1'b1) && (past_valid_r == 1'b1) && ($past(reset_i) == 1'b0) && !$past(enq_i)) |->
          (wptr_r_o == $past(wptr_r_o))));

      assert_rptr_advances_after_deq: assert property (@(posedge clk_i) disable iff (reset_i)
        (((model_known_r == 1'b1) && (past_valid_r == 1'b1) && ($past(reset_i) == 1'b0) && $past(deq_i)) |->
          (rptr_r_o == next_ptr($past(rptr_r_o)))));

      assert_rptr_stable_without_deq: assert property (@(posedge clk_i) disable iff (reset_i)
        (((model_known_r == 1'b1) && (past_valid_r == 1'b1) && ($past(reset_i) == 1'b0) && !$past(deq_i)) |->
          (rptr_r_o == $past(rptr_r_o))));
    end
  endgenerate

  generate
    if (els_p > 1) begin : bounded_pointer_properties
      assert_wptr_in_range: assert property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) |-> (wptr_r_o < els_p)));

      assert_rptr_in_range: assert property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) |-> (rptr_r_o < els_p)));

      assert_rptr_n_in_range: assert property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) |-> (rptr_n_o < els_p)));

      cover_wptr_boundary: cover property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) && (wptr_r_o == (els_p-1))));

      cover_rptr_boundary: cover property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) && (rptr_r_o == (els_p-1))));
    end
    else begin : degenerate_pointer_properties
      assert_wptr_zero_when_degenerate: assert property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) |-> (wptr_r_o == '0)));

      assert_rptr_zero_when_degenerate: assert property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) |-> (rptr_r_o == '0)));

      assert_rptr_n_zero_when_degenerate: assert property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) |-> (rptr_n_o == '0)));

      cover_degenerate_pointer_state: cover property (@(posedge clk_i) disable iff (reset_i)
        ((model_known_r == 1'b1) && (wptr_r_o == '0) && (rptr_r_o == '0)));
    end
  endgenerate

  generate
    if (els_p > 0) begin : occupancy_properties
      logic [count_width_lp-1:0] count_r = '0;
      logic count_known_r = 1'b0;

      always_ff @(posedge clk_i) begin
        if (reset_i) begin
          count_known_r <= 1'b1;
          count_r <= '0;
        end
        else if (count_known_r == 1'b1) begin
          case ({enq_i, deq_i})
            2'b10: count_r <= count_r + 1'b1;
            2'b01: count_r <= count_r - 1'b1;
            default: count_r <= count_r;
          endcase
        end
      end

      assert_count_never_exceeds_capacity: assert property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) |-> (count_r <= els_p)));

      assert_empty_matches_occupancy_zero: assert property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) |-> (empty_o == (count_r == '0))));

      assert_full_matches_occupancy_capacity: assert property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) |-> (full_o == (count_r == els_p))));

      assert_no_full_and_empty_with_capacity: assert property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) |-> !(full_o && empty_o)));

      assert_equal_ptrs_at_empty_or_full: assert property (@(posedge clk_i) disable iff (reset_i)
        (((count_known_r == 1'b1) && ((count_r == '0) || (count_r == els_p))) |-> (wptr_r_o == rptr_r_o)));

      assert_unequal_ptrs_when_partially_occupied: assert property (@(posedge clk_i) disable iff (reset_i)
        (((count_known_r == 1'b1) && (count_r > '0) && (count_r < els_p)) |-> (wptr_r_o != rptr_r_o)));

      cover_empty_state_observed: cover property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) && empty_o));

      cover_full_state_observed: cover property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) && full_o));

      cover_enq_accepted_when_not_full: cover property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) && enq_i && !full_o));

      cover_deq_accepted_when_not_empty: cover property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) && deq_i && !empty_o));

      cover_simultaneous_enq_deq_legal: cover property (@(posedge clk_i) disable iff (reset_i)
        ((count_known_r == 1'b1) && enq_i && deq_i && !full_o && !empty_o));
    end
  endgenerate

endmodule

bind bsg_fifo_tracker bsg_fifo_tracker_assertions #(.els_p(els_p), .ptr_width_lp(ptr_width_lp)) i_assertions (.*);