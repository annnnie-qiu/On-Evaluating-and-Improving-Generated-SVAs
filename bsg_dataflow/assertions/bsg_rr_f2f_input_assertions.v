module bsg_rr_f2f_input_assertions #(
  parameter width_p = 8,
  parameter num_in_p = 0,
  parameter middle_meet_p = 0,
  parameter middle_meet_data_lp = middle_meet_p * width_p,
  parameter min_in_middle_meet_p = (((num_in_p)<(middle_meet_p)) ? (num_in_p) : (middle_meet_p))
) (
  input clk,
  input reset,
  input [num_in_p-1:0] valid_i,
  input [width_p-1:0] data_i [num_in_p-1:0],
  input [width_p-1:0] data_head_o [middle_meet_p-1:0],
  input [middle_meet_p-1:0] valid_head_o,
  input [min_in_middle_meet_p-1:0] go_channels_i,
  input [$clog2(min_in_middle_meet_p+1)-1:0] go_cnt_i,
  input [num_in_p-1:0] yumi_o
);

  generate
    if ((num_in_p > 0) && (middle_meet_p > 0) && (width_p > 0)) begin : active_assertion_region
      localparam int ptr_width_lp = (((num_in_p == 1) || (num_in_p == 0)) ? 1 : $clog2(num_in_p));
      localparam int cnt_width_lp = ((min_in_middle_meet_p <= 1) ? 1 : $clog2(min_in_middle_meet_p+1));
      localparam int sum_width_lp = ptr_width_lp + cnt_width_lp + 1;
      localparam int data_head_flat_width_lp = width_p * middle_meet_p;

      logic [ptr_width_lp-1:0] ptr_shadow_r;
      logic [ptr_width_lp-1:0] ptr_shadow_n;
      logic [sum_width_lp-1:0] ptr_sum;
      logic [middle_meet_p-1:0] expected_valid_head;
      logic [num_in_p-1:0] expected_yumi;
      logic [data_head_flat_width_lp-1:0] expected_data_head_flat;
      logic [data_head_flat_width_lp-1:0] data_head_o_flat_obs;

      function automatic [cnt_width_lp-1:0] count_ones_go(input [min_in_middle_meet_p-1:0] vec);
        integer c;
        begin
          count_ones_go = '0;
          for (c = 0; c < min_in_middle_meet_p; c = c + 1) begin
            count_ones_go = count_ones_go + vec[c];
          end
        end
      endfunction

      function automatic [cnt_width_lp-1:0] count_ones_yumi(input [num_in_p-1:0] vec);
        integer c;
        begin
          count_ones_yumi = '0;
          for (c = 0; c < num_in_p; c = c + 1) begin
            count_ones_yumi = count_ones_yumi + vec[c];
          end
        end
      endfunction

      genvar data_flat_g;
      for (data_flat_g = 0; data_flat_g < middle_meet_p; data_flat_g = data_flat_g + 1) begin : flatten_observed_data_head
        assign data_head_o_flat_obs[(data_flat_g*width_p) +: width_p] = data_head_o[data_flat_g];
      end

      always_comb begin : auxiliary_ptr_next_logic
        ptr_sum = {{(cnt_width_lp+1){1'b0}}, ptr_shadow_r} + {{(ptr_width_lp+1){1'b0}}, go_cnt_i};
        if (num_in_p <= 1) begin
          ptr_shadow_n = '0;
        end
        else if (ptr_sum >= num_in_p) begin
          ptr_shadow_n = ptr_sum - num_in_p;
        end
        else begin
          ptr_shadow_n = ptr_sum[ptr_width_lp-1:0];
        end
      end

      always_ff @(posedge clk) begin : auxiliary_rtl_logic
        if (reset) begin
          ptr_shadow_r <= '0;
        end
        else begin
          ptr_shadow_r <= ptr_shadow_n;
        end
      end

      always_comb begin : expected_output_model
        integer idx;
        integer src;
        integer ch;
        integer off;

        expected_valid_head = '0;
        expected_yumi = '0;
        expected_data_head_flat = '0;

        for (idx = 0; idx < middle_meet_p; idx = idx + 1) begin
          if (idx < num_in_p) begin
            src = idx + int'(ptr_shadow_r);
            if (src >= num_in_p) begin
              src = src - num_in_p;
            end
            expected_valid_head[idx] = valid_i[src];
            expected_data_head_flat[(idx*width_p) +: width_p] = data_i[src];
          end
        end

        for (ch = 0; ch < num_in_p; ch = ch + 1) begin
          off = ch - int'(ptr_shadow_r);
          if (off < 0) begin
            off = off + num_in_p;
          end
          if (off < min_in_middle_meet_p) begin
            expected_yumi[ch] = go_channels_i[off];
          end
        end
      end

      begin : input_protocol_assumptions
        assume_go_cnt_in_range: assume property (@(posedge clk) disable iff (reset)
          (go_cnt_i <= min_in_middle_meet_p));

        assume_go_cnt_matches_channels: assume property (@(posedge clk) disable iff (reset)
          (go_cnt_i == count_ones_go(go_channels_i)));
      end

      begin : output_rotation_assertions
        assert_valid_head_matches_rotation: assert property (@(posedge clk) disable iff (reset)
          (valid_head_o == expected_valid_head));

        assert_data_head_matches_rotation: assert property (@(posedge clk) disable iff (reset)
          (data_head_o_flat_obs == expected_data_head_flat));

        assert_yumi_matches_go_rotation: assert property (@(posedge clk) disable iff (reset)
          (yumi_o == expected_yumi));

        assert_yumi_count_matches_go_cnt: assert property (@(posedge clk) disable iff (reset)
          (count_ones_yumi(yumi_o) == go_cnt_i));
      end

      if (middle_meet_p > num_in_p) begin : upper_zero_extension_assertions
        localparam int upper_valid_width_lp = middle_meet_p - num_in_p;
        localparam int upper_data_width_lp = width_p * (middle_meet_p - num_in_p);

        assert_valid_head_upper_zero: assert property (@(posedge clk) disable iff (reset)
          (valid_head_o[middle_meet_p-1:num_in_p] == {upper_valid_width_lp{1'b0}}));

        assert_data_head_upper_zero: assert property (@(posedge clk) disable iff (reset)
          (data_head_o_flat_obs[(width_p*num_in_p) +: upper_data_width_lp] == {upper_data_width_lp{1'b0}}));
      end

      begin : activity_covers
        cover_valid_head_nonzero: cover property (@(posedge clk) disable iff (reset)
          (valid_head_o != {middle_meet_p{1'b0}}));

        cover_yumi_nonzero: cover property (@(posedge clk) disable iff (reset)
          (yumi_o != {num_in_p{1'b0}}));

        cover_data_head_nonzero: cover property (@(posedge clk) disable iff (reset)
          (data_head_o_flat_obs != {data_head_flat_width_lp{1'b0}}));

        cover_simultaneous_valid_and_yumi: cover property (@(posedge clk) disable iff (reset)
          ((valid_head_o != {middle_meet_p{1'b0}}) && (yumi_o != {num_in_p{1'b0}})));
      end

      if (num_in_p > 1) begin : pointer_activity_covers
        cover_nonzero_pointer_with_output: cover property (@(posedge clk) disable iff (reset)
          ((ptr_shadow_r != {ptr_width_lp{1'b0}}) && ((valid_head_o != {middle_meet_p{1'b0}}) || (data_head_o_flat_obs != {data_head_flat_width_lp{1'b0}}))));

        cover_wrap_with_yumi: cover property (@(posedge clk) disable iff (reset)
          ((ptr_sum >= num_in_p) && (yumi_o != {num_in_p{1'b0}})));
      end
    end
    else begin : degenerate_parameter_region
    end
  endgenerate

endmodule

bind bsg_rr_f2f_input bsg_rr_f2f_input_assertions #(.width_p(width_p), .num_in_p(num_in_p), .middle_meet_p(middle_meet_p), .middle_meet_data_lp(middle_meet_data_lp), .min_in_middle_meet_p(min_in_middle_meet_p)) i_assertions (.*);