module bsg_flatten_2D_array_assertions #(
    parameter width_p = 8,
    parameter items_p = 8
) (
    input [width_p-1:0] i [items_p-1:0],
    input [width_p*items_p-1:0] o
);

  genvar item_idx;
  generate
    for (item_idx = 0; item_idx < items_p; item_idx = item_idx + 1) begin : gen_item_assertions
      always_comb begin : item_comb_assertions
        a_o_slice_matches_i: assert (o[item_idx*width_p +: width_p] == i[item_idx]);
        c_item_input_nonzero: cover (i[item_idx] != {width_p{1'b0}});
        c_item_input_all_ones: cover (i[item_idx] == {width_p{1'b1}});
      end
    end : gen_item_assertions
  endgenerate

endmodule

bind bsg_flatten_2D_array bsg_flatten_2D_array_assertions #(.width_p(width_p), .items_p(items_p)) i_assertions (.*);