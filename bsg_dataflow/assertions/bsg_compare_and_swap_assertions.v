module bsg_compare_and_swap_assertions
  #(parameter width_p = 8
    , parameter t_p = width_p-1
    , parameter b_p = 0
    , parameter cond_swap_on_equal_p = 0
    )
   (input [1:0] [width_p-1:0] data_i
    , input swap_on_equal_i
    , input [1:0] [width_p-1:0] data_o
    , input swapped_o
    );

  wire gt_expected = (data_i[0][t_p:b_p] > data_i[1][t_p:b_p]);
  wire eq_expected = (data_i[0][t_p:b_p] == data_i[1][t_p:b_p]);
  wire lt_expected = (data_i[0][t_p:b_p] < data_i[1][t_p:b_p]);
  wire cond_swap_on_equal_enabled = (cond_swap_on_equal_p != 0);

  wire expected_swapped = cond_swap_on_equal_enabled
                          ? (gt_expected | (eq_expected & swap_on_equal_i))
                          : gt_expected;

  wire [1:0] [width_p-1:0] expected_data_o = expected_swapped
                                             ? {data_i[0], data_i[1]}
                                             : {data_i[1], data_i[0]};

  always_comb begin : output_value_assertions
    a_swapped_matches_expected: assert (swapped_o == expected_swapped);
    a_data_matches_expected: assert (data_o == expected_data_o);
    a_output_compare_window_sorted: assert (data_o[0][t_p:b_p] <= data_o[1][t_p:b_p]);
    a_output_is_input_permutation: assert (((data_o[0] == data_i[0]) && (data_o[1] == data_i[1]))
                                           || ((data_o[0] == data_i[1]) && (data_o[1] == data_i[0])));
    a_swapped_path_data_mapping: assert (!swapped_o
                                         || ((data_o[0] == data_i[1]) && (data_o[1] == data_i[0])));
    a_not_swapped_path_data_mapping: assert (swapped_o
                                             || ((data_o[0] == data_i[0]) && (data_o[1] == data_i[1])));
    a_lower_output_is_compare_minimum: assert (gt_expected
                                               ? (data_o[0][t_p:b_p] == data_i[1][t_p:b_p])
                                               : (data_o[0][t_p:b_p] == data_i[0][t_p:b_p]));
    a_upper_output_is_compare_maximum: assert (gt_expected
                                               ? (data_o[1][t_p:b_p] == data_i[0][t_p:b_p])
                                               : (data_o[1][t_p:b_p] == data_i[1][t_p:b_p]));
  end

  generate
    if (cond_swap_on_equal_p != 0) begin : gen_cond_swap_on_equal_enabled
      always_comb begin : mode_assertions
        a_enabled_greater_always_swaps: assert (!gt_expected || swapped_o);
        a_enabled_less_never_swaps: assert (!lt_expected || !swapped_o);
        a_enabled_equal_follows_swap_on_equal: assert (!eq_expected || (swapped_o == swap_on_equal_i));
        a_enabled_swapped_equation: assert (swapped_o == (gt_expected | (eq_expected & swap_on_equal_i)));
      end

      always_comb begin : mode_covers
        c_enabled_greater_swap: cover (gt_expected && swapped_o);
        c_enabled_less_no_swap: cover (lt_expected && !swapped_o);
        c_enabled_equal_swap_taken: cover (eq_expected && swap_on_equal_i && swapped_o);
        c_enabled_equal_swap_not_taken: cover (eq_expected && !swap_on_equal_i && !swapped_o);
      end
    end
    else begin : gen_cond_swap_on_equal_disabled
      always_comb begin : mode_assertions
        a_disabled_swapped_is_greater: assert (swapped_o == gt_expected);
        a_disabled_greater_always_swaps: assert (!gt_expected || swapped_o);
        a_disabled_less_never_swaps: assert (!lt_expected || !swapped_o);
        a_disabled_equal_never_swaps: assert (!eq_expected || !swapped_o);
      end

      always_comb begin : mode_covers
        c_disabled_greater_swap: cover (gt_expected && swapped_o);
        c_disabled_less_no_swap: cover (lt_expected && !swapped_o);
        c_disabled_equal_no_swap: cover (eq_expected && !swapped_o);
      end
    end
  endgenerate

  always_comb begin : functional_covers
    c_swapped_high: cover (swapped_o);
    c_swapped_low: cover (!swapped_o);
    c_swap_path_outputs_reversed: cover (swapped_o && (data_o[0] == data_i[1]) && (data_o[1] == data_i[0]));
    c_no_swap_path_outputs_preserved: cover (!swapped_o && (data_o[0] == data_i[0]) && (data_o[1] == data_i[1]));
    c_output_compare_window_equal: cover (data_o[0][t_p:b_p] == data_o[1][t_p:b_p]);
  end

endmodule

bind bsg_compare_and_swap bsg_compare_and_swap_assertions #(.width_p(width_p), .t_p(t_p), .b_p(b_p), .cond_swap_on_equal_p(cond_swap_on_equal_p)) i_assertions (.*);