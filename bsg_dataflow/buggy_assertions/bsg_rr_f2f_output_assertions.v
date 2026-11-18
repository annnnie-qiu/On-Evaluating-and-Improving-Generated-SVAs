module bsg_rr_f2f_output_assertions #(
  parameter width_p = 8,
  parameter num_out_p = 8,
  parameter middle_meet_p = 8,
  parameter min_out_middle_meet_lp = (((num_out_p)<(middle_meet_p)) ? (num_out_p) : (middle_meet_p))
) (
  input clk,
  input reset,
  input [num_out_p-1:0] ready_i,
  input [middle_meet_p-1:0] ready_head_o,
  input [min_out_middle_meet_lp-1:0] go_channels_i,
  input [$clog2(min_out_middle_meet_lp+1)-1:0] go_cnt_i,
  input [width_p-1:0] data_head_i[min_out_middle_meet_lp-1:0],
  input [num_out_p-1:0] valid_o,
  input [width_p-1:0] data_o[num_out_p-1:0]
);

  localparam ptr_width_lp = ((((num_out_p)==1) || ((num_out_p)==0)) ? 1 : $clog2(num_out_p));

  function automatic [ptr_width_lp-1:0] advance_ptr(
    input [ptr_width_lp-1:0] ptr_i,
    input [$clog2(min_out_middle_meet_lp+1)-1:0] add_i
  );
    int unsigned sum_v;
    begin
      if (num_out_p <= 1) begin
        advance_ptr = '0;
      end
      else begin
        sum_v = ptr_i;
        sum_v = sum_v + add_i;
        advance_ptr = sum_v % num_out_p;
      end
    end
  endfunction

  function automatic [middle_meet_p-1:0] expected_ready_head(
    input [ptr_width_lp-1:0] ptr_i,
    input [num_out_p-1:0] ready_v
  );
    int bit_idx;
    int src_idx;
    begin
      expected_ready_head = '0;
      for (bit_idx = 0; bit_idx < middle_meet_p; bit_idx = bit_idx + 1) begin
        if (bit_idx < num_out_p) begin
          src_idx = bit_idx + ptr_i;
          if (src_idx >= num_out_p) begin
            src_idx = src_idx - num_out_p;
          end
          expected_ready_head[bit_idx] = ready_v[src_idx];
        end
      end
    end
  endfunction

  function automatic [num_out_p-1:0] expected_valid_vec(
    input [ptr_width_lp-1:0] ptr_i,
    input [min_out_middle_meet_lp-1:0] channels_v
  );
    int bit_idx;
    int src_idx;
    begin
      expected_valid_vec = '0;
      for (bit_idx = 0; bit_idx < num_out_p; bit_idx = bit_idx + 1) begin
        src_idx = bit_idx + num_out_p - ptr_i;
        if (src_idx >= num_out_p) begin
          src_idx = src_idx - num_out_p;
        end
        if (src_idx < min_out_middle_meet_lp) begin
          expected_valid_vec[bit_idx] = channels_v[src_idx];
        end
      end
    end
  endfunction

  function automatic [width_p-1:0] expected_data_element(
    input int out_idx_i,
    input [ptr_width_lp-1:0] ptr_i
  );
    int src_idx;
    begin
      expected_data_element = '0;
      src_idx = out_idx_i + num_out_p - ptr_i;
      if (src_idx >= num_out_p) begin
        src_idx = src_idx - num_out_p;
      end
      if (src_idx < min_out_middle_meet_lp) begin
        expected_data_element = data_head_i[src_idx];
      end
    end
  endfunction

  begin : auxiliary_rtl_logic
    bit reset_seen;
    bit model_valid;
    logic [ptr_width_lp-1:0] model_ptr;
    logic [ptr_width_lp-1:0] model_ptr_data;

    always_ff @(posedge clk) begin
      if (reset) begin
        reset_seen <= 1'b1;
        model_valid <= 1'b0;
        model_ptr <= '0;
        model_ptr_data <= '0;
      end
      else if (reset_seen && !model_valid) begin
        model_valid <= 1'b1;
        model_ptr <= advance_ptr('0, go_cnt_i);
        model_ptr_data <= advance_ptr('0, go_cnt_i);
      end
      else if (model_valid) begin
        model_ptr <= advance_ptr(model_ptr, go_cnt_i);
        model_ptr_data <= advance_ptr(model_ptr_data, go_cnt_i);
      end
    end

    logic [num_out_p*width_p-1:0] data_o_flat;
    logic [num_out_p*width_p-1:0] expected_data_o_flat;

    genvar data_idx;
    for (data_idx = 0; data_idx < num_out_p; data_idx = data_idx + 1) begin : gen_data_flatten
      assign data_o_flat[(data_idx*width_p)+:width_p] = data_o[data_idx];
      assign expected_data_o_flat[(data_idx*width_p)+:width_p] = expected_data_element(data_idx, model_ptr_data);
    end

    begin : input_protocol_assumptions
      assume_go_cnt_i_in_range: assume property (@(posedge clk) disable iff (reset)
        (go_cnt_i <= min_out_middle_meet_lp)
      );
    end

    begin : output_mapping_assertions
      assert_ready_head_o_matches_model: assert property (@(posedge clk) disable iff (reset || !model_valid)
        (ready_head_o == expected_ready_head(model_ptr, ready_i))
      );

      assert_valid_o_matches_model: assert property (@(posedge clk) disable iff (reset || !model_valid)
        (valid_o == expected_valid_vec(model_ptr, go_channels_i))
      );

      assert_data_o_matches_model: assert property (@(posedge clk) disable iff (reset || !model_valid)
        (data_o_flat == expected_data_o_flat)
      );
    end

    generate
      if (middle_meet_p > num_out_p) begin : ready_head_upper_zero_assertions
        assert_ready_head_o_upper_zero: assert property (@(posedge clk) disable iff (reset || !model_valid)
          (ready_head_o[middle_meet_p-1:num_out_p] == '0)
        );
      end
      else begin : ready_head_no_upper_zero_assertions
        assert_ready_head_o_full_width_used: assert property (@(posedge clk) disable iff (reset || !model_valid)
          (ready_head_o == expected_ready_head(model_ptr, ready_i))
        );
      end
    endgenerate

    begin : output_activity_covers
      cover_valid_o_nonzero: cover property (@(posedge clk) disable iff (reset || !model_valid)
        (valid_o != '0)
      );

      cover_valid_o_zero: cover property (@(posedge clk) disable iff (reset || !model_valid)
        (valid_o == '0)
      );

      cover_ready_head_o_nonzero: cover property (@(posedge clk) disable iff (reset || !model_valid)
        (ready_head_o != '0)
      );

      cover_data_o_nonzero: cover property (@(posedge clk) disable iff (reset || !model_valid)
        (data_o_flat != '0)
      );
    end
  end

endmodule

bind bsg_rr_f2f_output bsg_rr_f2f_output_assertions #(.width_p(width_p), .num_out_p(num_out_p), .middle_meet_p(middle_meet_p), .min_out_middle_meet_lp(min_out_middle_meet_lp)) i_assertions (.*);