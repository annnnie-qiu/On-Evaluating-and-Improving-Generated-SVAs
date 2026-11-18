module bsg_two_buncher_assertions #(parameter width_p = 8)
   (input                  clk_i
    , input                reset_i
    , input  [width_p-1:0] data_i
    , input                v_i
    , input                ready_and_o
    , input  [width_p*2-1:0] data_o
    , input  [1:0]         v_o
    , input  [1:0]         ready_and_i
    );

  // The downstream double-ready interface is defined only for ordered ready bits.
  // It is an environment obligation that ready_and_i[1] is not asserted unless
  // ready_and_i[0] is also asserted.
  begin : input_protocol_assumptions
    assume_ready_and_i_ordered: assume property (@(posedge clk_i) disable iff (reset_i)
      ready_and_i[1] |-> ready_and_i[0]
    );
  end

  begin : combinational_output_assertions
    assert_data_upper_passthrough: assert property (@(posedge clk_i) disable iff (reset_i)
      data_o[width_p*2-1:width_p] == data_i
    );

    assert_v_o_upper_implies_lower: assert property (@(posedge clk_i) disable iff (reset_i)
      v_o[1] |-> v_o[0]
    );

    assert_input_valid_implies_lower_valid: assert property (@(posedge clk_i) disable iff (reset_i)
      v_i |-> v_o[0]
    );

    assert_no_input_implies_no_upper_valid: assert property (@(posedge clk_i) disable iff (reset_i)
      !v_i |-> !v_o[1]
    );

    assert_ready_high_when_downstream_ready0: assert property (@(posedge clk_i) disable iff (reset_i)
      ready_and_i[0] |-> ready_and_o
    );

    assert_ready_with_input_matches_inferred_buffer: assert property (@(posedge clk_i) disable iff (reset_i)
      v_i |-> (ready_and_o == (ready_and_i[0] | ~v_o[1]))
    );

    assert_ready_without_input_matches_inferred_buffer: assert property (@(posedge clk_i) disable iff (reset_i)
      !v_i |-> (ready_and_o == (ready_and_i[0] | ~v_o[0]))
    );

    assert_empty_lower_data_with_input: assert property (@(posedge clk_i) disable iff (reset_i)
      (v_i && !v_o[1]) |-> (data_o[width_p-1:0] == data_i)
    );

    assert_empty_lower_data_without_input: assert property (@(posedge clk_i) disable iff (reset_i)
      (!v_i && !v_o[0]) |-> (data_o[width_p-1:0] == data_i)
    );
  end

  begin : temporal_state_assertions
    assert_empty_capture_visible_next: assert property (@(posedge clk_i) disable iff (reset_i)
      (v_i && !ready_and_i[0] && ready_and_o)
      |=> ((data_o[width_p-1:0] == $past(data_i))
           && (v_o == {v_i, 1'b1})
           && (ready_and_o == ready_and_i[0]))
    );

    assert_full_replace_visible_next: assert property (@(posedge clk_i) disable iff (reset_i)
      (v_i && v_o[1] && ready_and_i[0] && !ready_and_i[1])
      |=> ((data_o[width_p-1:0] == $past(data_i))
           && (v_o == {v_i, 1'b1})
           && (ready_and_o == ready_and_i[0]))
    );

    assert_full_hold_stalled_visible_next: assert property (@(posedge clk_i) disable iff (reset_i)
      ((((v_i && v_o[1]) || (!v_i && v_o[0]))) && !ready_and_i[0])
      |=> ((data_o[width_p-1:0] == $past(data_o[width_p-1:0]))
           && (v_o == {v_i, 1'b1})
           && (ready_and_o == ready_and_i[0]))
    );

    assert_full_drain_no_input_next_empty: assert property (@(posedge clk_i) disable iff (reset_i)
      (!v_i && v_o[0] && ready_and_i[0])
      |=> ((ready_and_o == 1'b1)
           && (v_o == {1'b0, v_i})
           && (data_o[width_p-1:0] == data_i))
    );

    assert_full_drain_two_next_empty: assert property (@(posedge clk_i) disable iff (reset_i)
      (v_i && v_o[1] && ready_and_i[1])
      |=> ((ready_and_o == 1'b1)
           && (v_o == {1'b0, v_i})
           && (data_o[width_p-1:0] == data_i))
    );

    assert_empty_passthrough_next_empty: assert property (@(posedge clk_i) disable iff (reset_i)
      (v_i && !v_o[1] && ready_and_i[0])
      |=> ((ready_and_o == 1'b1)
           && (v_o == {1'b0, v_i})
           && (data_o[width_p-1:0] == data_i))
    );

    assert_empty_idle_next_empty: assert property (@(posedge clk_i) disable iff (reset_i)
      (!v_i && !v_o[0])
      |=> ((ready_and_o == 1'b1)
           && (v_o == {1'b0, v_i})
           && (data_o[width_p-1:0] == data_i))
    );
  end

  begin : interface_covers
    cover_empty_idle: cover property (@(posedge clk_i) disable iff (reset_i)
      (!v_i && !v_o[0] && ready_and_o)
    );

    cover_empty_capture: cover property (@(posedge clk_i) disable iff (reset_i)
      (v_i && !ready_and_i[0] && ready_and_o)
    );

    cover_empty_passthrough: cover property (@(posedge clk_i) disable iff (reset_i)
      (v_i && ready_and_i[0] && !v_o[1] && ready_and_o && (v_o == 2'b01))
    );

    cover_full_hold_stalled: cover property (@(posedge clk_i) disable iff (reset_i)
      ((((v_i && v_o[1]) || (!v_i && v_o[0]))) && !ready_and_i[0] && !ready_and_o)
    );

    cover_full_drain_no_input: cover property (@(posedge clk_i) disable iff (reset_i)
      (!v_i && v_o[0] && ready_and_i[0] && ready_and_o)
    );

    cover_full_replace_one: cover property (@(posedge clk_i) disable iff (reset_i)
      (v_i && v_o[1] && ready_and_i[0] && !ready_and_i[1] && ready_and_o)
    );

    cover_full_drain_two: cover property (@(posedge clk_i) disable iff (reset_i)
      (v_i && v_o[1] && ready_and_i[1] && ready_and_o && (v_o == 2'b11))
    );

    cover_capture_then_buffered_output: cover property (@(posedge clk_i) disable iff (reset_i)
      (v_i && !ready_and_i[0] && ready_and_o)
      ##1 (v_o[0] && (data_o[width_p-1:0] == $past(data_i)))
    );
  end

endmodule

bind bsg_two_buncher bsg_two_buncher_assertions #(.width_p(width_p)) i_assertions (.*);