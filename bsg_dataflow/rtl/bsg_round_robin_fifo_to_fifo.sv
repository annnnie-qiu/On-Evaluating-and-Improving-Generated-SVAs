module bsg_round_robin_fifo_to_fifo
  #(parameter width_p = 8
    ,parameter num_in_p = 8
    ,parameter num_out_p=1

    // bitvector; set bit "X" if you want to
    // support a mode where a subset X of channels are enabled
    // note each bit set adds a couple of shifters
    // so this is not free. default is to not support any subsets

    ,parameter in_channel_count_mask_p = (1 << (num_in_p-1))
    ,parameter out_channel_count_mask_p = (1 << (num_out_p-1))
    )
   (input clk, input  reset

    // input side
    , input  [num_in_p-1:0] valid_i
    , input  [width_p-1:0]  data_i [num_in_p-1:0]
    , output [num_in_p-1:0] yumi_o

    // high water mark of how many total channels are activated.
    // e.g; if one channel is activated, it would be at 0.
    , input  [((($clog2(num_in_p)-1)>(0)) ? ($clog2(num_in_p)-1) : (0)):0]  in_top_channel_i
    , input  [((($clog2(num_out_p)-1)>(0)) ? ($clog2(num_out_p)-1) : (0)):0] out_top_channel_i

    // output side
    , output [num_out_p-1:0]   valid_o
    , output [width_p-1:0]     data_o [num_out_p-1:0]
    , input  [num_out_p-1:0]   ready_i
    );

   localparam middle_meet_lp= (((num_in_p)<(num_out_p)) ? (num_in_p) : (num_out_p));

   wire [middle_meet_lp-1:0]   go_channels;

   wire [middle_meet_lp*width_p-1:0] data_head  [num_in_p-1:0];
   wire [middle_meet_lp-1:0]         valid_head [num_in_p-1:0];
   wire [middle_meet_lp-1:0]         ready_head [num_out_p-1:0];
   wire [num_in_p-1:0]         yumi_int_o [num_in_p-1:0];

   wire [num_out_p-1:0]         valid_int_o[num_out_p-1:0];
   wire [width_p*num_out_p-1:0] data_int_o[num_out_p-1:0];

   // this is for supporting variable numbers of active
   // input and output channels

   assign yumi_o  = yumi_int_o[in_top_channel_i];
   assign valid_o = valid_int_o[out_top_channel_i];

   typedef logic [width_p*num_out_p-1:0] bsg_round_robin_fifo_to_fifo_t;

   wire [width_p*num_out_p-1:0] data_o_flat = data_int_o[out_top_channel_i];
   bsg_round_robin_fifo_to_fifo_t zero_flat;

   wire [$clog2(middle_meet_lp+1)-1:0]   go_cnt;

   assign zero_flat = bsg_round_robin_fifo_to_fifo_t ' (0);

   // 2D array format converters
   // bsg_flatten_2D_array #(.width_p(width_p), .items_p(num_in_p)) bf2Da (.i(data_i),           .o(data_i_flat));
   bsg_make_2D_array #(.width_p(width_p), .items_p(num_out_p))
   bm2Da (.i(data_o_flat), .o(data_o));

   genvar                      i,j;

   // we generate separate input side logic to handle
   // different numbers of input channels
   for (i = 0; i < num_in_p; i++)
     begin: ic
        if (in_channel_count_mask_p[i])
          begin : in_chan
             wire [width_p-1:0] data_head_tmp [middle_meet_lp-1:0];
             // Pad the data_i / valid_i sub-views with zeros for the
             // unused upper channels.  Some VCS parameter-override flows
             // misapply the top-level num_in_p to the bsg_rr_f2f_input
             // sub-module, which makes its data_i port expect num_in_p
             // elements rather than (i+1).  By passing the FULL num_in_p
             // arrays (zero-padded above index i) the port type always
             // matches num_in_p elements, regardless of the actual
             // number of channels being driven.  The internal logic of
             // bsg_rr_f2f_input only reads up to (i+1)=num_in_p entries
             // so the padding does not affect functionality.
             wire [width_p-1:0] data_i_padded [num_in_p-1:0];
             wire [num_in_p-1:0] valid_i_padded;
             wire [num_in_p-1:0] yumi_o_padded;
             for (genvar k = 0; k < num_in_p; k++) begin : gen_pad_in
                if (k <= i) begin : gen_active
                   assign data_i_padded[k]  = data_i[k];
                   assign valid_i_padded[k] = valid_i[k];
                end else begin : gen_pad
                   assign data_i_padded[k]  = '0;
                   assign valid_i_padded[k] = 1'b0;
                end
             end

             //bsg_flatten_2D_array #(.width_p(width_p), .items_p(middle_meet_lp))
             //bf2Da (.i(data_head_tmp),.o(data_head[i]));

	     assign data_head[i] = { >> {data_head_tmp} };

             // INPUT SIDE (input: valid_i, data_i; middle input; go_channels)
             // Pass FULL num_in_p arrays (not slices) so the port type
             // matches whatever num_in_p the sub-module elaborates with
             // (which under some VCS flows ends up being the top
             // num_in_p instead of i+1).
             bsg_rr_f2f_input #(.width_p(width_p)
                                ,.num_in_p(num_in_p)
                                ,.middle_meet_p(middle_meet_lp)
                                ) bsg_rr_ff_in
               (.clk(clk), .reset(reset | (in_top_channel_i != i))
                , .valid_i(valid_i_padded)            // inputs (padded width)
                , .data_i(data_i_padded)              // (padded width)

                , .data_head_o(data_head_tmp)         // back to us
                ,. valid_head_o(valid_head[i])

                // The bsg_rr_f2f_input sub-module always elaborates with
                // (.num_in_p(num_in_p), .middle_meet_p(middle_meet_lp)),
                // so its go_channels_i / go_cnt_i ports are sized for the
                // FULL middle_meet_lp width. Older versions of this RTL
                // sliced go_channels / go_cnt to (i+1) bits, which works
                // for ic[middle_meet_lp-1] but cuts off bits for ic[i] with
                // i < middle_meet_lp-1 — making the sub-module's
                // assume_go_cnt_consistent (countones equality) get
                // falsified at parent-level FPV. Pass the full middle_meet_lp
                // width to keep the assume's countones equation structurally
                // valid for every ic[i].
                , .go_channels_i(go_channels)         // back to them (full width)
                , .go_cnt_i(go_cnt)
                , .yumi_o(yumi_o_padded)              // final output (padded width)
                );

             // Extract the active yumi bits from the padded output.
             for (genvar k = 0; k <= i; k++) begin : gen_pad_yumi
                assign yumi_int_o[i][k] = yumi_o_padded[k];
             end

	     // MBT: this is redundant with the cast inside bsg_rr_f2f_input
	     // and results in a synthesis warning
	     /*
             for (j = i+1; j < middle_meet_lp; j=j+1)
	       begin
                  assign valid_head[i][j] = 1'b0;
	       end
	      */

             for (j = i+1; j < num_in_p; j=j+1)
	       begin
                  assign yumi_int_o[i][j] = 1'b0;
	       end

          end // if (in_channel_count_mask_p[i])
     end // block: c

   // MIDDLE (ties INPUT to OUTPUT)

   bsg_rr_f2f_middle #(.width_p(width_p)
                       ,.middle_meet_p(middle_meet_lp)
                       ) brrf2fm
   (.valid_head_i(valid_head[in_top_channel_i])
    , .ready_head_i(ready_head[out_top_channel_i])
    , .go_channels_o(go_channels)
    , .go_cnt_o(go_cnt)
    );

   // OUTPUT SIDE

   for (i = 0; i < num_out_p; i++)
     begin: oc
        if (out_channel_count_mask_p[i])
              begin: out_chan
                 wire [width_p-1:0] data_head_array_padded [middle_meet_lp-1:0];
                 wire [width_p-1:0] data_o_array_padded [num_out_p-1:0];
                 wire [num_out_p-1:0] valid_o_padded;
                 wire [num_out_p-1:0] ready_i_padded;

                 // Pad ready_i to full num_out_p width.
                 for (genvar k = 0; k < num_out_p; k++) begin : gen_pad_rdy
                    if (k <= i) begin : gen_active
                       assign ready_i_padded[k] = ready_i[k];
                    end else begin : gen_pad
                       assign ready_i_padded[k] = 1'b0;
                    end
                 end

                 // Build full-width data_head_array (padded to middle_meet_lp)
                 // The sub-module expects middle_meet_p elements which under
                 // some VCS flows is the top-level middle_meet_lp value.
                 bsg_make_2D_array #(.width_p(width_p), .items_p(middle_meet_lp))
                 bm2Da (.i(data_head[in_top_channel_i][0+:middle_meet_lp*width_p])
                        ,.o(data_head_array_padded));

                 //bsg_flatten_2D_array #(.width_p(width_p), .items_p(i+1))
                 //bf2Da (.i(data_o_array), .o(data_int_o[i][width_p*(i+1)-1:0]));

                 // Map only the active (i+1) entries of data_o into data_int_o.
                 for (genvar k = 0; k <= i; k++) begin : gen_pack_data
                    assign data_int_o[i][width_p*(k+1)-1 -: width_p] = data_o_array_padded[k];
                 end

                 bsg_rr_f2f_output #(.width_p(width_p)
                                     ,.num_out_p(num_out_p)
                                     ,.middle_meet_p(middle_meet_lp)
                                     ) bsg_rr_ff_out
                   (.clk(clk), .reset(reset | (out_top_channel_i != i))
                    , .ready_i(ready_i_padded)
                    , .ready_head_o(ready_head[i])   // back to us

                    // bsg_rr_f2f_output has port go_channels_i / go_cnt_i
                    // sized to its min_out_middle_meet_lp = min(num_out_p,
                    // middle_meet_lp) which always equals middle_meet_lp
                    // for this top (since middle_meet_lp = min(num_in_p,
                    // num_out_p)). Pass the FULL middle_meet_lp width so the
                    // sub-module's go_channels / go_cnt assumes hold
                    // structurally for every oc[i].
                    , .go_channels_i(go_channels)         // back to them (full width)
                    , .go_cnt_i(go_cnt)
                    , .data_head_i(data_head_array_padded)

                    , .valid_o(valid_o_padded)             // final outputs (padded)
                    , .data_o(data_o_array_padded)         // (padded)
                    );

                 // Extract the active valid bits.
                 for (genvar k = 0; k <= i; k++) begin : gen_extract_valid
                    assign valid_int_o[i][k] = valid_o_padded[k];
                 end

                 for (j = i+1; j < num_out_p; j=j+1)
                   begin
                      assign valid_int_o[i][j] = 1'b0;
                   end
                 if (num_out_p - i > 1)
                   assign data_int_o[i][width_p*num_out_p-1:width_p*(i+1)]
                     = zero_flat[0+: (width_p*(num_out_p-(i+1)))];
              end // block: iff
     end // block: c

endmodule
