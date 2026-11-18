module bsg_fifo_bypass_assertions
  #(parameter width_p = 8
    , parameter logic ready_THEN_valid_p = 1'b0
    )
   (input logic [width_p-1:0] data_i
    , input logic v_i
    , input logic ready_param_o

    , input logic [width_p-1:0] data_o
    , input logic v_o
    , input logic yumi_i

    , input logic [width_p-1:0] fifo_data_o
    , input logic fifo_v_o
    , input logic fifo_ready_param_i

    , input logic [width_p-1:0] fifo_data_i
    , input logic fifo_v_i
    , input logic fifo_yumi_o
    );

  wire enq_expected = ready_THEN_valid_p ? v_i : (ready_param_o & v_i);

  always_comb begin : common_output_equation_checks
    assert_ready_param_o_matches_fifo_ready_param_i: assert (ready_param_o == fifo_ready_param_i);
    assert_fifo_data_o_matches_data_i: assert (fifo_data_o == data_i);
    assert_fifo_yumi_o_equation: assert (fifo_yumi_o == (fifo_v_i & yumi_i));
    assert_data_o_mux_equation: assert (data_o == (fifo_v_i ? fifo_data_i : data_i));
    assert_v_o_equation: assert (v_o == (fifo_v_i | enq_expected));
    assert_fifo_v_o_equation: assert (fifo_v_o == (enq_expected & (fifo_v_i | ~yumi_i)));
  end

  always_comb begin : common_behavior_checks
    assert_fifo_data_selected_when_fifo_valid: assert ((!fifo_v_i) || (data_o == fifo_data_i));
    assert_bypass_data_selected_when_fifo_empty: assert (fifo_v_i || (data_o == data_i));
    assert_fifo_yumi_only_on_fifo_valid: assert ((!fifo_yumi_o) || fifo_v_i);
    assert_fifo_yumi_only_when_output_consumed: assert ((!fifo_yumi_o) || yumi_i);
    assert_fifo_valid_forces_output_valid: assert ((!fifo_v_i) || v_o);
    assert_enqueue_forces_output_valid: assert ((!enq_expected) || v_o);
    assert_fifo_v_o_requires_enqueue: assert ((!fifo_v_o) || enq_expected);
    assert_fifo_v_o_requires_fifo_valid_or_no_yumi: assert ((!fifo_v_o) || fifo_v_i || !yumi_i);
    assert_no_fifo_v_o_without_buffer_need: assert ((enq_expected && (fifo_v_i || !yumi_i)) || !fifo_v_o);
  end

  always_comb begin : common_cover_points
    cover_ready_param_high: cover (ready_param_o && fifo_ready_param_i);
    cover_ready_param_low: cover ((!ready_param_o) && (!fifo_ready_param_i));
    cover_direct_bypass_output_valid: cover ((!fifo_v_i) && v_o && (data_o == data_i));
    cover_fifo_buffered_output_valid: cover (fifo_v_i && v_o && (data_o == fifo_data_i));
    cover_fifo_yumi_fire: cover (fifo_yumi_o && fifo_v_i && yumi_i);
    cover_fifo_enqueue_output_valid: cover (fifo_v_o && enq_expected);
    cover_bypass_without_fifo_enqueue_when_consumed: cover ((!fifo_v_i) && enq_expected && yumi_i && v_o && (!fifo_v_o));
  end

  generate
    if (ready_THEN_valid_p) begin : gen_ready_then_valid_mode
      always_comb begin : ready_then_valid_output_checks
        assert_rtv_v_o_equation: assert (v_o == (fifo_v_i | v_i));
        assert_rtv_fifo_v_o_equation: assert (fifo_v_o == (v_i & (fifo_v_i | ~yumi_i)));
        assert_rtv_input_valid_forces_output_valid: assert ((!v_i) || v_o);
        assert_rtv_fifo_v_o_independent_of_ready_when_needed: assert ((!(v_i && (fifo_v_i || !yumi_i))) || fifo_v_o);
        assert_rtv_no_fifo_v_o_when_no_input_valid: assert (v_i || !fifo_v_o);
      end

      always_comb begin : ready_then_valid_cover_points
        cover_rtv_valid_when_not_ready_still_outputs: cover (v_i && (!ready_param_o) && v_o);
        cover_rtv_valid_when_not_ready_enqueues_if_needed: cover (v_i && (!ready_param_o) && (fifo_v_i || !yumi_i) && fifo_v_o);
        cover_rtv_empty_consumed_bypass_no_enqueue: cover (v_i && (!fifo_v_i) && yumi_i && v_o && (!fifo_v_o));
      end
    end
    else begin : gen_ready_and_valid_mode
      always_comb begin : ready_and_valid_output_checks
        assert_rav_v_o_equation: assert (v_o == (fifo_v_i | (ready_param_o & v_i)));
        assert_rav_fifo_v_o_equation: assert (fifo_v_o == ((ready_param_o & v_i) & (fifo_v_i | ~yumi_i)));
        assert_rav_no_enqueue_when_not_ready: assert (ready_param_o || (!fifo_v_o));
        assert_rav_no_new_output_when_empty_and_not_ready: assert (fifo_v_i || ready_param_o || (!v_o));
        assert_rav_ready_and_valid_forces_output_valid: assert ((!(ready_param_o && v_i)) || v_o);
      end

      always_comb begin : ready_and_valid_cover_points
        cover_rav_ready_valid_output: cover (ready_param_o && v_i && v_o);
        cover_rav_valid_not_ready_no_fifo_no_output: cover (v_i && (!ready_param_o) && (!fifo_v_i) && (!v_o));
        cover_rav_ready_valid_enqueues_if_needed: cover (ready_param_o && v_i && (fifo_v_i || !yumi_i) && fifo_v_o);
        cover_rav_empty_consumed_bypass_no_enqueue: cover (ready_param_o && v_i && (!fifo_v_i) && yumi_i && v_o && (!fifo_v_o));
      end
    end
  endgenerate

endmodule

bind bsg_fifo_bypass bsg_fifo_bypass_assertions #(.width_p(width_p), .ready_THEN_valid_p(ready_THEN_valid_p)) i_assertions (.*);