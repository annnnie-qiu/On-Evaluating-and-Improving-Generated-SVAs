module bsg_fifo_1r1w_multi_fifo_head_assertions
  #(parameter lg_total_els_lp = 8)
   (input clk_i
    ,input reset_i

    ,input enque_i
    ,input [lg_total_els_lp-1:0] enque_addr_i

    ,input enque_addr_v_o
    ,input [lg_total_els_lp-1:0] enque_addr_o

    ,input deque_i

    ,input deque_addr_v_o
    ,input [lg_total_els_lp-1:0] deque_addr_o

    ,input [lg_total_els_lp-1:0] deque_data_i

    ,input v_o
    );

  wire equal_now = (enque_addr_o == deque_addr_o);

  generate
    begin : combinational_output_properties
      ap_enque_addr_v_matches_valid_enque: assert property (@(posedge clk_i) disable iff (reset_i)
        enque_addr_v_o == (v_o & enque_i));

      ap_deque_addr_v_matches_multielement_deque: assert property (@(posedge clk_i) disable iff (reset_i)
        deque_addr_v_o == (deque_i & v_o & ~equal_now));

      ap_deque_addr_v_implies_valid: assert property (@(posedge clk_i) disable iff (reset_i)
        deque_addr_v_o |-> v_o);

      ap_deque_addr_v_implies_not_equal: assert property (@(posedge clk_i) disable iff (reset_i)
        deque_addr_v_o |-> (enque_addr_o != deque_addr_o));

      ap_no_deque_addr_v_when_empty: assert property (@(posedge clk_i) disable iff (reset_i)
        (!v_o) |-> (!deque_addr_v_o));
    end

    begin : sequential_state_properties
      ap_v_next_state: assert property (@(posedge clk_i) disable iff (reset_i)
        $past(!reset_i) |->
          (v_o == ($past(enque_i)
                   || ($past(v_o)
                       && !($past(deque_i)
                            && ($past(enque_addr_o) == $past(deque_addr_o)))))));

      ap_v_set_after_enque: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(enque_i)) |-> v_o);

      ap_v_clear_after_singleton_deque_without_enque: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i)
         && $past(v_o)
         && $past(deque_i)
         && !$past(enque_i)
         && ($past(enque_addr_o) == $past(deque_addr_o))) |-> (!v_o));

      ap_v_hold_after_no_enque_no_singleton_deque: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i)
         && !$past(enque_i)
         && !($past(v_o) && $past(deque_i) && ($past(enque_addr_o) == $past(deque_addr_o)))) |->
          (v_o == $past(v_o)));

      ap_tail_load_on_enque: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(enque_i)) |->
          (enque_addr_o == $past(enque_addr_i)));

      ap_tail_hold_without_enque: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && !$past(enque_i)) |->
          (enque_addr_o == $past(enque_addr_o)));

      ap_deque_addr_bypass_after_pointer_read: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i) && $past(deque_addr_v_o)) |->
          (deque_addr_o == deque_data_i));

      ap_deque_addr_load_on_empty_or_singleton_enque: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i)
         && $past(enque_i)
         && ($past(!v_o) || ($past(enque_addr_o == deque_addr_o) && $past(deque_i)))) |->
          (deque_addr_o == $past(enque_addr_i)));

      ap_deque_addr_hold_without_head_update: assert property (@(posedge clk_i) disable iff (reset_i)
        ($past(!reset_i)
         && $past(!reset_i, 2)
         && !$past(deque_addr_v_o)
         && !$past(deque_addr_v_o, 2)
         && !$past(enque_i && (!v_o || ((enque_addr_o == deque_addr_o) && deque_i)))) |->
          (deque_addr_o == $past(deque_addr_o)));
    end

    begin : coverage_properties
      cp_enque_addr_valid_event: cover property (@(posedge clk_i) disable iff (reset_i)
        enque_addr_v_o);

      cp_deque_addr_valid_event: cover property (@(posedge clk_i) disable iff (reset_i)
        deque_addr_v_o);

      cp_empty_enque_event: cover property (@(posedge clk_i) disable iff (reset_i)
        (!v_o && enque_i));

      cp_singleton_deque_to_empty_event: cover property (@(posedge clk_i) disable iff (reset_i)
        (v_o && equal_now && deque_i && !enque_i) ##1 (!v_o));

      cp_singleton_replace_event: cover property (@(posedge clk_i) disable iff (reset_i)
        (v_o && equal_now && deque_i && enque_i) ##1 (v_o && (deque_addr_o == $past(enque_addr_i))));

      cp_multielement_deque_bypass_event: cover property (@(posedge clk_i) disable iff (reset_i)
        (deque_addr_v_o) ##1 (deque_addr_o == deque_data_i));

      cp_tail_load_event: cover property (@(posedge clk_i) disable iff (reset_i)
        (enque_i) ##1 (enque_addr_o == $past(enque_addr_i)));

      cp_v_becomes_valid_event: cover property (@(posedge clk_i) disable iff (reset_i)
        (!v_o && enque_i) ##1 v_o);
    end
  endgenerate

endmodule

bind bsg_fifo_1r1w_multi_fifo_head bsg_fifo_1r1w_multi_fifo_head_assertions #(.lg_total_els_lp(lg_total_els_lp)) i_assertions (.*);