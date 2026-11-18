module bsg_sort_4_assertions #(
  parameter width_p = 8,
  parameter items_p = 4,
  parameter t_p = width_p-1,
  parameter b_p = 0
) (
  input [width_p-1:0] i [items_p-1:0],
  input [width_p-1:0] o [items_p-1:0]
);

  function automatic [2:0] count_eq_word4;
    input [width_p-1:0] ref;
    input [width_p-1:0] d0;
    input [width_p-1:0] d1;
    input [width_p-1:0] d2;
    input [width_p-1:0] d3;
    begin
      count_eq_word4 = ((d0 == ref) ? 3'd1 : 3'd0)
                     + ((d1 == ref) ? 3'd1 : 3'd0)
                     + ((d2 == ref) ? 3'd1 : 3'd0)
                     + ((d3 == ref) ? 3'd1 : 3'd0);
    end
  endfunction

  function automatic [2:0] count_lt_key4;
    input [width_p-1:0] ref;
    input [width_p-1:0] d0;
    input [width_p-1:0] d1;
    input [width_p-1:0] d2;
    input [width_p-1:0] d3;
    begin
      count_lt_key4 = ((d0[t_p:b_p] < ref[t_p:b_p]) ? 3'd1 : 3'd0)
                    + ((d1[t_p:b_p] < ref[t_p:b_p]) ? 3'd1 : 3'd0)
                    + ((d2[t_p:b_p] < ref[t_p:b_p]) ? 3'd1 : 3'd0)
                    + ((d3[t_p:b_p] < ref[t_p:b_p]) ? 3'd1 : 3'd0);
    end
  endfunction

  function automatic [2:0] count_le_key4;
    input [width_p-1:0] ref;
    input [width_p-1:0] d0;
    input [width_p-1:0] d1;
    input [width_p-1:0] d2;
    input [width_p-1:0] d3;
    begin
      count_le_key4 = ((d0[t_p:b_p] <= ref[t_p:b_p]) ? 3'd1 : 3'd0)
                    + ((d1[t_p:b_p] <= ref[t_p:b_p]) ? 3'd1 : 3'd0)
                    + ((d2[t_p:b_p] <= ref[t_p:b_p]) ? 3'd1 : 3'd0)
                    + ((d3[t_p:b_p] <= ref[t_p:b_p]) ? 3'd1 : 3'd0);
    end
  endfunction

  function automatic bit keys_distinct4;
    input [width_p-1:0] d0;
    input [width_p-1:0] d1;
    input [width_p-1:0] d2;
    input [width_p-1:0] d3;
    begin
      keys_distinct4 = (d0[t_p:b_p] != d1[t_p:b_p])
                    && (d0[t_p:b_p] != d2[t_p:b_p])
                    && (d0[t_p:b_p] != d3[t_p:b_p])
                    && (d1[t_p:b_p] != d2[t_p:b_p])
                    && (d1[t_p:b_p] != d3[t_p:b_p])
                    && (d2[t_p:b_p] != d3[t_p:b_p]);
    end
  endfunction

  function automatic bit member_word4;
    input [width_p-1:0] ref;
    input [width_p-1:0] d0;
    input [width_p-1:0] d1;
    input [width_p-1:0] d2;
    input [width_p-1:0] d3;
    begin
      member_word4 = (ref == d0) || (ref == d1) || (ref == d2) || (ref == d3);
    end
  endfunction

  function automatic [4*width_p-1:0] stable_sort4;
    input [width_p-1:0] d0;
    input [width_p-1:0] d1;
    input [width_p-1:0] d2;
    input [width_p-1:0] d3;
    reg [width_p-1:0] a0;
    reg [width_p-1:0] a1;
    reg [width_p-1:0] a2;
    reg [width_p-1:0] a3;
    reg [width_p-1:0] tmp;
    begin
      a0 = d0;
      a1 = d1;
      a2 = d2;
      a3 = d3;

      if (a0[t_p:b_p] > a1[t_p:b_p]) begin
        tmp = a0;
        a0 = a1;
        a1 = tmp;
      end
      if (a1[t_p:b_p] > a2[t_p:b_p]) begin
        tmp = a1;
        a1 = a2;
        a2 = tmp;
      end
      if (a2[t_p:b_p] > a3[t_p:b_p]) begin
        tmp = a2;
        a2 = a3;
        a3 = tmp;
      end
      if (a0[t_p:b_p] > a1[t_p:b_p]) begin
        tmp = a0;
        a0 = a1;
        a1 = tmp;
      end
      if (a1[t_p:b_p] > a2[t_p:b_p]) begin
        tmp = a1;
        a1 = a2;
        a2 = tmp;
      end
      if (a0[t_p:b_p] > a1[t_p:b_p]) begin
        tmp = a0;
        a0 = a1;
        a1 = tmp;
      end

      stable_sort4 = {a3, a2, a1, a0};
    end
  endfunction

  generate
    if (items_p == 4) begin : gen_supported_items_4
      always_comb begin : comb_sort_properties
        assert_output_sorted_0_1: assert (o[1][t_p:b_p] >= o[0][t_p:b_p]);
        assert_output_sorted_1_2: assert (o[2][t_p:b_p] >= o[1][t_p:b_p]);
        assert_output_sorted_2_3: assert (o[3][t_p:b_p] >= o[2][t_p:b_p]);

        assert_output0_global_min_key: assert ((o[0][t_p:b_p] <= i[0][t_p:b_p])
                                            && (o[0][t_p:b_p] <= i[1][t_p:b_p])
                                            && (o[0][t_p:b_p] <= i[2][t_p:b_p])
                                            && (o[0][t_p:b_p] <= i[3][t_p:b_p]));
        assert_output3_global_max_key: assert ((o[3][t_p:b_p] >= i[0][t_p:b_p])
                                            && (o[3][t_p:b_p] >= i[1][t_p:b_p])
                                            && (o[3][t_p:b_p] >= i[2][t_p:b_p])
                                            && (o[3][t_p:b_p] >= i[3][t_p:b_p]));

        assert_output0_rank_bounds: assert ((count_lt_key4(o[0], i[0], i[1], i[2], i[3]) <= 3'd0)
                                         && (count_le_key4(o[0], i[0], i[1], i[2], i[3]) >= 3'd1));
        assert_output1_rank_bounds: assert ((count_lt_key4(o[1], i[0], i[1], i[2], i[3]) <= 3'd1)
                                         && (count_le_key4(o[1], i[0], i[1], i[2], i[3]) >= 3'd2));
        assert_output2_rank_bounds: assert ((count_lt_key4(o[2], i[0], i[1], i[2], i[3]) <= 3'd2)
                                         && (count_le_key4(o[2], i[0], i[1], i[2], i[3]) >= 3'd3));
        assert_output3_rank_bounds: assert ((count_lt_key4(o[3], i[0], i[1], i[2], i[3]) <= 3'd3)
                                         && (count_le_key4(o[3], i[0], i[1], i[2], i[3]) >= 3'd4));

        assert_output0_member_of_input: assert (member_word4(o[0], i[0], i[1], i[2], i[3]));
        assert_output1_member_of_input: assert (member_word4(o[1], i[0], i[1], i[2], i[3]));
        assert_output2_member_of_input: assert (member_word4(o[2], i[0], i[1], i[2], i[3]));
        assert_output3_member_of_input: assert (member_word4(o[3], i[0], i[1], i[2], i[3]));

        assert_data_multiset_preserved: assert ((count_eq_word4(i[0], o[0], o[1], o[2], o[3]) == count_eq_word4(i[0], i[0], i[1], i[2], i[3]))
                                            && (count_eq_word4(i[1], o[0], o[1], o[2], o[3]) == count_eq_word4(i[1], i[0], i[1], i[2], i[3]))
                                            && (count_eq_word4(i[2], o[0], o[1], o[2], o[3]) == count_eq_word4(i[2], i[0], i[1], i[2], i[3]))
                                            && (count_eq_word4(i[3], o[0], o[1], o[2], o[3]) == count_eq_word4(i[3], i[0], i[1], i[2], i[3])));

        assert_full_stable_sort_reference: assert ({o[3], o[2], o[1], o[0]} == stable_sort4(i[0], i[1], i[2], i[3]));
        assert_distinct_keys_unique_sorted_reference: assert ((!keys_distinct4(i[0], i[1], i[2], i[3]))
                                                           || ({o[3], o[2], o[1], o[0]} == stable_sort4(i[0], i[1], i[2], i[3])));

        cover_output_keys_strictly_increasing: cover ((o[0][t_p:b_p] < o[1][t_p:b_p])
                                                   && (o[1][t_p:b_p] < o[2][t_p:b_p])
                                                   && (o[2][t_p:b_p] < o[3][t_p:b_p]));
        cover_output_has_adjacent_equal_keys: cover ((o[0][t_p:b_p] == o[1][t_p:b_p])
                                                  || (o[1][t_p:b_p] == o[2][t_p:b_p])
                                                  || (o[2][t_p:b_p] == o[3][t_p:b_p]));
        cover_output_all_keys_equal: cover ((o[0][t_p:b_p] == o[1][t_p:b_p])
                                         && (o[1][t_p:b_p] == o[2][t_p:b_p])
                                         && (o[2][t_p:b_p] == o[3][t_p:b_p]));
        cover_output_order_differs_from_input_order: cover ({o[3], o[2], o[1], o[0]} != {i[3], i[2], i[1], i[0]});
      end
    end : gen_supported_items_4
  endgenerate

endmodule

bind bsg_sort_4 bsg_sort_4_assertions #(.width_p(width_p), .items_p(items_p), .t_p(t_p), .b_p(b_p)) i_assertions (.*);