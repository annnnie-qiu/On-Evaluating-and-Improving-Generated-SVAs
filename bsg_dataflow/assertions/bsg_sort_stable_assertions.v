module bsg_sort_stable_assertions #(
  parameter width_p = 8,
  parameter items_p = 4,
  parameter t_p = width_p-1,
  parameter b_p = 0
) (
  input [width_p-1:0] i [items_p-1:0],
  input [width_p-1:0] o [items_p-1:0]
);

  function automatic logic key_le(input logic [width_p-1:0] a, input logic [width_p-1:0] b);
    key_le = (a[t_p:b_p] <= b[t_p:b_p]);
  endfunction

  function automatic logic key_lt(input logic [width_p-1:0] a, input logic [width_p-1:0] b);
    key_lt = (a[t_p:b_p] < b[t_p:b_p]);
  endfunction

  function automatic logic key_eq(input logic [width_p-1:0] a, input logic [width_p-1:0] b);
    key_eq = (a[t_p:b_p] == b[t_p:b_p]);
  endfunction

  function automatic logic stable_precedes(
    input logic [width_p-1:0] a,
    input logic [1:0] a_idx,
    input logic [width_p-1:0] b,
    input logic [1:0] b_idx
  );
    stable_precedes = key_lt(a, b) || (key_eq(a, b) && (a_idx < b_idx));
  endfunction

  function automatic logic is_permutation4(
    input logic [width_p-1:0] out0,
    input logic [width_p-1:0] out1,
    input logic [width_p-1:0] out2,
    input logic [width_p-1:0] out3,
    input logic [width_p-1:0] in0,
    input logic [width_p-1:0] in1,
    input logic [width_p-1:0] in2,
    input logic [width_p-1:0] in3
  );
    is_permutation4 =
      ((out0 == in0) && (out1 == in1) && (out2 == in2) && (out3 == in3)) ||
      ((out0 == in0) && (out1 == in1) && (out2 == in3) && (out3 == in2)) ||
      ((out0 == in0) && (out1 == in2) && (out2 == in1) && (out3 == in3)) ||
      ((out0 == in0) && (out1 == in2) && (out2 == in3) && (out3 == in1)) ||
      ((out0 == in0) && (out1 == in3) && (out2 == in1) && (out3 == in2)) ||
      ((out0 == in0) && (out1 == in3) && (out2 == in2) && (out3 == in1)) ||
      ((out0 == in1) && (out1 == in0) && (out2 == in2) && (out3 == in3)) ||
      ((out0 == in1) && (out1 == in0) && (out2 == in3) && (out3 == in2)) ||
      ((out0 == in1) && (out1 == in2) && (out2 == in0) && (out3 == in3)) ||
      ((out0 == in1) && (out1 == in2) && (out2 == in3) && (out3 == in0)) ||
      ((out0 == in1) && (out1 == in3) && (out2 == in0) && (out3 == in2)) ||
      ((out0 == in1) && (out1 == in3) && (out2 == in2) && (out3 == in0)) ||
      ((out0 == in2) && (out1 == in0) && (out2 == in1) && (out3 == in3)) ||
      ((out0 == in2) && (out1 == in0) && (out2 == in3) && (out3 == in1)) ||
      ((out0 == in2) && (out1 == in1) && (out2 == in0) && (out3 == in3)) ||
      ((out0 == in2) && (out1 == in1) && (out2 == in3) && (out3 == in0)) ||
      ((out0 == in2) && (out1 == in3) && (out2 == in0) && (out3 == in1)) ||
      ((out0 == in2) && (out1 == in3) && (out2 == in1) && (out3 == in0)) ||
      ((out0 == in3) && (out1 == in0) && (out2 == in1) && (out3 == in2)) ||
      ((out0 == in3) && (out1 == in0) && (out2 == in2) && (out3 == in1)) ||
      ((out0 == in3) && (out1 == in1) && (out2 == in0) && (out3 == in2)) ||
      ((out0 == in3) && (out1 == in1) && (out2 == in2) && (out3 == in0)) ||
      ((out0 == in3) && (out1 == in2) && (out2 == in0) && (out3 == in1)) ||
      ((out0 == in3) && (out1 == in2) && (out2 == in1) && (out3 == in0));
  endfunction

  generate
    if (items_p == 4) begin : gen_items_4
      logic [width_p-1:0] ref_o [3:0];
      logic [1:0] ref_idx [3:0];
      logic [width_p-1:0] tmp_o;
      logic [1:0] tmp_idx;
      integer sort_m;
      integer sort_n;

      always_comb begin : stable_reference_and_output_checks
        ref_o[0] = i[0];
        ref_o[1] = i[1];
        ref_o[2] = i[2];
        ref_o[3] = i[3];
        ref_idx[0] = 2'd0;
        ref_idx[1] = 2'd1;
        ref_idx[2] = 2'd2;
        ref_idx[3] = 2'd3;
        tmp_o = '0;
        tmp_idx = 2'd0;

        for (sort_m = 0; sort_m < 4; sort_m = sort_m + 1) begin
          for (sort_n = sort_m + 1; sort_n < 4; sort_n = sort_n + 1) begin
            if (stable_precedes(ref_o[sort_n], ref_idx[sort_n], ref_o[sort_m], ref_idx[sort_m])) begin
              tmp_o = ref_o[sort_m];
              tmp_idx = ref_idx[sort_m];
              ref_o[sort_m] = ref_o[sort_n];
              ref_idx[sort_m] = ref_idx[sort_n];
              ref_o[sort_n] = tmp_o;
              ref_idx[sort_n] = tmp_idx;
            end
          end
        end

        assert_output_order_0_1: assert (key_le(o[0], o[1]));
        assert_output_order_1_2: assert (key_le(o[1], o[2]));
        assert_output_order_2_3: assert (key_le(o[2], o[3]));

        assert_output_is_input_permutation: assert (is_permutation4(o[0], o[1], o[2], o[3], i[0], i[1], i[2], i[3]));

        assert_output_0_is_global_minimum: assert (key_le(o[0], i[0]) && key_le(o[0], i[1]) && key_le(o[0], i[2]) && key_le(o[0], i[3]));
        assert_output_3_is_global_maximum: assert (key_le(i[0], o[3]) && key_le(i[1], o[3]) && key_le(i[2], o[3]) && key_le(i[3], o[3]));

        assert_stable_reference_output_0: assert (o[0] == ref_o[0]);
        assert_stable_reference_output_1: assert (o[1] == ref_o[1]);
        assert_stable_reference_output_2: assert (o[2] == ref_o[2]);
        assert_stable_reference_output_3: assert (o[3] == ref_o[3]);

        cover_identity_sorted_path: cover ((o[0] == i[0]) && (o[1] == i[1]) && (o[2] == i[2]) && (o[3] == i[3]) && key_le(i[0], i[1]) && key_le(i[1], i[2]) && key_le(i[2], i[3]));
        cover_any_reordering_path: cover ((o[0] != i[0]) || (o[1] != i[1]) || (o[2] != i[2]) || (o[3] != i[3]));
        cover_equal_adjacent_output_keys: cover (key_eq(o[1], o[2]));
      end
    end
  endgenerate

endmodule

bind bsg_sort_stable bsg_sort_stable_assertions #(.width_p(width_p), .items_p(items_p), .t_p(t_p), .b_p(b_p)) i_assertions (.*);