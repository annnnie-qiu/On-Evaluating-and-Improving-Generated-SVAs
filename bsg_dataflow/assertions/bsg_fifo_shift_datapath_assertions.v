module bsg_fifo_shift_datapath_assertions
  #(parameter width_p = 8
    , parameter els_p = 8
    , parameter default_p = { (width_p) {1'b0} }
    )
   (input clk_i
    , input [width_p-1:0] data_i
    , input [els_p-1:0][1:0] sel_i
    , input [width_p-1:0] data_o
    );

  logic [els_p:0][width_p-1:0] shadow_r = '0;
  logic [els_p:0] shadow_valid = '0;
  logic [els_p-1:0] sel_is_shift;
  logic all_sel_shift;

  assign shadow_r[els_p] = default_p;
  assign shadow_valid[els_p] = 1'b1;

  genvar aux_i;
  generate
    for (aux_i = 0; aux_i < els_p; aux_i = aux_i + 1) begin : auxiliary_rtl_logic
      assign sel_is_shift[aux_i] = (sel_i[aux_i] == 2'b01);

      always_ff @(posedge clk_i) begin
        unique case (sel_i[aux_i])
          2'b01: begin
            shadow_r[aux_i] <= shadow_r[aux_i+1];
            shadow_valid[aux_i] <= shadow_valid[aux_i+1];
          end
          2'b10: begin
            shadow_r[aux_i] <= data_i;
            shadow_valid[aux_i] <= 1'b1;
          end
          default: begin
            shadow_r[aux_i] <= shadow_r[aux_i];
            shadow_valid[aux_i] <= shadow_valid[aux_i];
          end
        endcase
      end
    end : auxiliary_rtl_logic
  endgenerate

  assign all_sel_shift = &sel_is_shift;

  begin : output_update_assertions
    a_sel0_load_updates_output: assert property (@(posedge clk_i)
      (sel_i[0] == 2'b10) |=> (data_o == $past(data_i))
    );

    a_sel0_keep00_preserves_output: assert property (@(posedge clk_i)
      (sel_i[0] == 2'b00) |=> (data_o == $past(data_o))
    );

    a_sel0_keep11_preserves_output: assert property (@(posedge clk_i)
      (sel_i[0] == 2'b11) |=> (data_o == $past(data_o))
    );

    a_sel0_shift_known_next_to_output: assert property (@(posedge clk_i)
      ((sel_i[0] == 2'b01) && shadow_valid[1]) |=> (data_o == $past(shadow_r[1]))
    );

    a_known_shadow_output_matches: assert property (@(posedge clk_i)
      shadow_valid[0] |-> (data_o == shadow_r[0])
    );

    a_all_shift_flushes_default: assert property (@(posedge clk_i)
      all_sel_shift[*els_p] |=> (data_o == default_p)
    );

    c_sel0_load_updates_output: cover property (@(posedge clk_i)
      (sel_i[0] == 2'b10) |=> (data_o == $past(data_i))
    );

    c_sel0_keep00_preserves_output: cover property (@(posedge clk_i)
      (sel_i[0] == 2'b00) |=> (data_o == $past(data_o))
    );

    c_sel0_keep11_preserves_output: cover property (@(posedge clk_i)
      (sel_i[0] == 2'b11) |=> (data_o == $past(data_o))
    );

    c_sel0_shift_known_next_to_output: cover property (@(posedge clk_i)
      ((sel_i[0] == 2'b01) && shadow_valid[1]) |=> (data_o == $past(shadow_r[1]))
    );

    c_known_shadow_output_matches: cover property (@(posedge clk_i)
      shadow_valid[0] && (data_o == shadow_r[0])
    );

    c_all_shift_flushes_default: cover property (@(posedge clk_i)
      all_sel_shift[*els_p] |=> (data_o == default_p)
    );
  end : output_update_assertions

endmodule

bind bsg_fifo_shift_datapath bsg_fifo_shift_datapath_assertions #(.width_p(width_p), .els_p(els_p), .default_p(default_p)) i_assertions (.*);