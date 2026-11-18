module bsg_permute_box_assertions #(
    parameter width_p = 8,
    parameter items_p = 8,
    parameter lg_items_lp = $bits(items_p)
) (
    input [width_p-1:0] data_i [items_p-1:0],
    input [lg_items_lp-1:0] select_i [items_p-1:0],
    input [width_p-1:0] data_o [items_p-1:0]
);

  localparam integer select_values_lp = (lg_items_lp >= 31) ? 2147483647 : (1 << lg_items_lp);

  genvar out_idx;
  genvar in_idx;
  genvar other_idx;

  generate
    for (out_idx = 0; out_idx < items_p; out_idx = out_idx + 1) begin : gen_per_output

      always_comb begin : select_range_assumption
        a_select_within_data_i_range: assume (select_i[out_idx] < items_p);
      end

      always_comb begin : output_mux_assertions
        a_data_o_equals_selected_data_i: assert (data_o[out_idx] == data_i[select_i[out_idx]]);
        c_selected_data_relation_observed: cover (data_o[out_idx] == data_i[select_i[out_idx]]);
        c_output_nonzero_observed: cover (|data_o[out_idx]);
        c_output_zero_observed: cover (data_o[out_idx] == {width_p{1'b0}});
      end

      for (in_idx = 0; in_idx < items_p; in_idx = in_idx + 1) begin : gen_per_input_choice
        if (in_idx < select_values_lp) begin : gen_representable_input_choice
          localparam [lg_items_lp-1:0] in_idx_lp = in_idx;

          always_comb begin : selected_choice_assertions
            a_selected_choice_routes_correctly: assert ((select_i[out_idx] != in_idx_lp) || (data_o[out_idx] == data_i[in_idx]));
            c_selected_choice_observed: cover ((select_i[out_idx] == in_idx_lp) && (data_o[out_idx] == data_i[in_idx]));
          end
        end
      end

      for (other_idx = 0; other_idx < items_p; other_idx = other_idx + 1) begin : gen_per_output_pair
        if (other_idx > out_idx) begin : gen_distinct_output_pair
          always_comb begin : same_select_consistency_assertions
            a_equal_select_outputs_match: assert ((select_i[out_idx] != select_i[other_idx]) || (data_o[out_idx] == data_o[other_idx]));
            c_equal_select_outputs_match_observed: cover ((select_i[out_idx] == select_i[other_idx]) && (data_o[out_idx] == data_o[other_idx]));
          end
        end
      end

    end
  endgenerate

endmodule

bind bsg_permute_box bsg_permute_box_assertions #(.width_p(width_p), .items_p(items_p), .lg_items_lp(lg_items_lp)) i_assertions (.*);