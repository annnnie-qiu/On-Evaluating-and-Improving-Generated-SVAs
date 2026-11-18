module bsg_make_2D_array_assertions #(
  parameter width_p = 8,
  parameter items_p = 8
)(
  input [width_p*items_p-1:0] i,
  input [width_p-1:0] o [items_p-1:0]
);

  genvar item_idx;

  generate
    for (item_idx = 0; item_idx < items_p; item_idx = item_idx + 1) begin : per_item_assertions
      always_comb begin : comb_item_checks
        a_o_matches_i_slice: assert (o[item_idx] == i[item_idx*width_p +: width_p]);
        c_o_all_zero: cover (o[item_idx] == {width_p{1'b0}});
        c_o_all_one: cover (o[item_idx] == {width_p{1'b1}});
      end
    end
  endgenerate

endmodule

bind bsg_make_2D_array bsg_make_2D_array_assertions #(.width_p(width_p), .items_p(items_p)) i_assertions (.*);