module bsg_one_fifo_assertions #(parameter width_p = 8)
  (input clk_i
   , input reset_i
   , input ready_and_o
   , input [width_p-1:0] data_i
   , input v_i
   , input v_o
   , input [width_p-1:0] data_o
   , input yumi_i
   );

  generate
    if (1) begin : output_relation_properties
      a_ready_valid_complement: assert property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o == ~v_o));

      a_no_simultaneous_ready_and_valid: assert property (@(posedge clk_i) disable iff (reset_i)
        !(ready_and_o && v_o));

      a_ready_or_valid_always_high: assert property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o || v_o));
    end

    if (1) begin : state_transition_properties
      a_next_valid_matches_fifo_update: assert property (@(posedge clk_i) disable iff (reset_i)
        (1'b1) |=> ($past(!reset_i) && (v_o == ($past(v_o) ? ~$past(yumi_i) : $past(v_i)))));

      a_next_ready_matches_fifo_update: assert property (@(posedge clk_i) disable iff (reset_i)
        (1'b1) |=> ($past(!reset_i) && (ready_and_o == ~(($past(v_o) ? ~$past(yumi_i) : $past(v_i))))));

      a_empty_no_push_stays_empty: assert property (@(posedge clk_i) disable iff (reset_i)
        (!v_o && !v_i) |=> ($past(!reset_i) && !v_o && ready_and_o));

      a_empty_push_becomes_full: assert property (@(posedge clk_i) disable iff (reset_i)
        (!v_o && v_i) |=> ($past(!reset_i) && v_o && !ready_and_o));

      a_full_no_yumi_stays_full: assert property (@(posedge clk_i) disable iff (reset_i)
        (v_o && !yumi_i) |=> ($past(!reset_i) && v_o && !ready_and_o));

      a_full_yumi_becomes_empty: assert property (@(posedge clk_i) disable iff (reset_i)
        (v_o && yumi_i) |=> ($past(!reset_i) && !v_o && ready_and_o));
    end

    if (1) begin : data_path_properties
      a_next_data_matches_load_enable: assert property (@(posedge clk_i) disable iff (reset_i)
        (1'b1) |=> ($past(!reset_i) && (($past(v_i) && $past(ready_and_o)) ? (data_o == $past(data_i)) : (data_o == $past(data_o)))));

      a_accepted_input_visible_next: assert property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o && v_i) |=> ($past(!reset_i) && v_o && (data_o == $past(data_i))));

      a_empty_push_captures_data: assert property (@(posedge clk_i) disable iff (reset_i)
        (!v_o && v_i) |=> ($past(!reset_i) && (data_o == $past(data_i))));

      a_no_accept_holds_data: assert property (@(posedge clk_i) disable iff (reset_i)
        (!(ready_and_o && v_i)) |=> ($past(!reset_i) && (data_o == $past(data_o))));

      a_full_no_yumi_holds_data: assert property (@(posedge clk_i) disable iff (reset_i)
        (v_o && !yumi_i) |=> ($past(!reset_i) && (data_o == $past(data_o))));

      a_not_ready_holds_data: assert property (@(posedge clk_i) disable iff (reset_i)
        (!ready_and_o) |=> ($past(!reset_i) && (data_o == $past(data_o))));
    end

    if (1) begin : coverage_properties
      c_empty_state: cover property (@(posedge clk_i) disable iff (reset_i)
        (!v_o && ready_and_o));

      c_full_state: cover property (@(posedge clk_i) disable iff (reset_i)
        (v_o && !ready_and_o));

      c_input_accept: cover property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o && v_i));

      c_empty_no_push_antecedent: cover property (@(posedge clk_i) disable iff (reset_i)
        (!v_o && !v_i));

      c_full_hold_antecedent: cover property (@(posedge clk_i) disable iff (reset_i)
        (v_o && !yumi_i));

      c_output_yumi_antecedent: cover property (@(posedge clk_i) disable iff (reset_i)
        (v_o && yumi_i));

      c_fill_then_drain: cover property (@(posedge clk_i) disable iff (reset_i)
        (!v_o && ready_and_o && v_i) ##1 (v_o && !ready_and_o && yumi_i) ##1 (!v_o && ready_and_o));

      c_data_capture: cover property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o && v_i) ##1 (v_o && (data_o == $past(data_i))));

      c_data_hold_while_full: cover property (@(posedge clk_i) disable iff (reset_i)
        (v_o && !yumi_i) ##1 (v_o && (data_o == $past(data_o))));
    end
  endgenerate

endmodule

bind bsg_one_fifo bsg_one_fifo_assertions #(.width_p(width_p)) i_assertions (.*);