module bsg_serial_in_parallel_out_assertions
  #(parameter width_p = 8
    , parameter els_p = 8
    , parameter out_els_p = els_p
    )
   (input clk_i
    , input reset_i
    , input valid_i
    , input [width_p-1:0] data_i
    , input ready_and_o
    , input [out_els_p-1:0] valid_o
    , input [out_els_p-1:0][width_p-1:0] data_o
    , input [$clog2(out_els_p+1)-1:0] yumi_cnt_i
    );

  localparam count_width_lp = $clog2(els_p+1);
  localparam double_els_lp  = els_p * 2;

  logic [count_width_lp-1:0] aux_count_r, aux_count_n;
  logic [count_width_lp:0]   aux_visible_count;
  logic                      aux_enq;

  logic [els_p-1:0][width_p-1:0]        aux_data_r, aux_data_n;
  logic [double_els_lp-1:0][width_p-1:0] aux_data_ext;

  logic [out_els_p-1:0]                expected_valid_o;
  logic [out_els_p-1:0][width_p-1:0]   expected_data_o;

  function automatic logic data_matches_expected
    (input logic [out_els_p-1:0] exp_valid
     , input logic [out_els_p-1:0][width_p-1:0] actual_data
     , input logic [out_els_p-1:0][width_p-1:0] exp_data
     );
    logic result;
    begin
      result = 1'b1;
      for (int i = 0; i < out_els_p; i++) begin
        if (exp_valid[i]) begin
          result = result & (actual_data[i] == exp_data[i]);
        end
      end
      return result;
    end
  endfunction

  always_comb begin : auxiliary_rtl_logic_comb
    aux_enq = valid_i & (aux_count_r < els_p);
    aux_visible_count = aux_count_r + aux_enq;

    aux_data_ext = '0;
    for (int i = 0; i < els_p; i++) begin
      aux_data_ext[i] = aux_data_r[i];
    end

    if (aux_enq) begin
      aux_data_ext[aux_count_r] = data_i;
    end

    expected_valid_o = '0;
    expected_data_o  = '0;
    for (int i = 0; i < out_els_p; i++) begin
      expected_valid_o[i] = (i < aux_visible_count);
      expected_data_o[i]  = aux_data_ext[i];
    end

    aux_count_n = aux_visible_count - yumi_cnt_i;
    aux_data_n  = '0;
    for (int i = 0; i < els_p; i++) begin
      aux_data_n[i] = aux_data_ext[yumi_cnt_i+i];
    end
  end

  always_ff @(posedge clk_i) begin : auxiliary_rtl_logic_seq
    if (reset_i) begin
      aux_count_r <= '0;
      aux_data_r  <= '0;
    end
    else begin
      aux_count_r <= aux_count_n;
      aux_data_r  <= aux_data_n;
    end
  end

  begin : environment_assumptions
    a_yumi_cnt_within_output_range: assume property (@(posedge clk_i) disable iff (reset_i)
      (yumi_cnt_i <= out_els_p));

    a_yumi_cnt_not_exceed_available: assume property (@(posedge clk_i) disable iff (reset_i)
      (yumi_cnt_i <= aux_visible_count));
  end

  begin : output_assertions
    a_ready_matches_model: assert property (@(posedge clk_i) disable iff (reset_i)
      (ready_and_o == (aux_count_r < els_p)));

    a_valid_vector_matches_model: assert property (@(posedge clk_i) disable iff (reset_i)
      (valid_o == expected_valid_o));

    a_valid_data_matches_model: assert property (@(posedge clk_i) disable iff (reset_i)
      data_matches_expected(expected_valid_o, data_o, expected_data_o));

    a_empty_without_input_has_no_valid: assert property (@(posedge clk_i) disable iff (reset_i)
      ((aux_count_r == '0) && !valid_i) |-> (valid_o == '0));

    a_full_state_not_ready: assert property (@(posedge clk_i) disable iff (reset_i)
      (aux_count_r == els_p) |-> !ready_and_o);

    a_accepting_input_appears_at_tail: assert property (@(posedge clk_i) disable iff (reset_i)
      (valid_i && (aux_count_r < els_p) && (aux_count_r < out_els_p))
      |-> (valid_o[aux_count_r] && (data_o[aux_count_r] == data_i)));
  end

  begin : coverage_properties
    c_enqueue_fire: cover property (@(posedge clk_i) disable iff (reset_i)
      (valid_i && ready_and_o));

    c_dequeue_fire: cover property (@(posedge clk_i) disable iff (reset_i)
      (yumi_cnt_i != '0));

    c_simultaneous_enqueue_dequeue: cover property (@(posedge clk_i) disable iff (reset_i)
      (valid_i && ready_and_o && (yumi_cnt_i != '0)));

    c_fifo_full: cover property (@(posedge clk_i) disable iff (reset_i)
      ((aux_count_r == els_p) && !ready_and_o));

    c_empty_bypass_to_output: cover property (@(posedge clk_i) disable iff (reset_i)
      ((aux_count_r == '0) && valid_i && ready_and_o && valid_o[0] && (data_o[0] == data_i)));
  end

  generate
    if (out_els_p <= els_p) begin : output_capacity_covers
      c_all_output_slots_valid: cover property (@(posedge clk_i) disable iff (reset_i)
        (&valid_o));
    end
  endgenerate

endmodule

bind bsg_serial_in_parallel_out bsg_serial_in_parallel_out_assertions #(.width_p(width_p), .els_p(els_p), .out_els_p(out_els_p)) i_assertions (.*);