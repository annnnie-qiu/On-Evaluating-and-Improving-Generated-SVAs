module bsg_rr_f2f_input #(parameter width_p = 8
                          , parameter num_in_p             = 0
                          , parameter middle_meet_p        = 0
                          , parameter middle_meet_data_lp  = middle_meet_p * width_p
                          , parameter min_in_middle_meet_p = (((num_in_p)<(middle_meet_p)) ? (num_in_p) : (middle_meet_p))
                          )
   (input clk, input  reset

    // primary input
    , input  [num_in_p-1:0] valid_i
    , input  [width_p-1:0]  data_i [num_in_p-1:0]

    // to intermediate module; only send as many as middle will look at
    // note: middle_meet may be < num_in_p because num_out_p < num_in_p
    //       middle_meet may be > num_in_p because other input modules may have greater num_in_p

    , output [width_p-1:0]  data_head_o [middle_meet_p-1:0]
    , output [middle_meet_p-1:0] valid_head_o

    // from intermediate module
    , input  [min_in_middle_meet_p-1:0] go_channels_i

    // may be smaller than middle_meet because any sends are
    // limited by how many one's we output
    , input  [$clog2(min_in_middle_meet_p+1)-1:0] go_cnt_i

    // final output
    , output [num_in_p-1:0] yumi_o
    );

   logic [( (((num_in_p)==1) || ((num_in_p)==0))? 1 : $clog2((num_in_p)))-1:0] iptr_r, iptr_r_data;

   wire [width_p*num_in_p-1:0]      data_i_flat = ({ >> {data_i} });
   wire [width_p*middle_meet_p-1:0] data_head_o_flat;

   // 2D array format converters
   //bsg_flatten_2D_array #(.width_p(width_p), .items_p(num_in_p))
   //bf2Da (.i(data_i),           .o(data_i_flat));

   bsg_make_2D_array    #(.width_p(width_p), .items_p(middle_meet_p))
   bm2Da (.i(data_head_o_flat), .o(data_head_o));

   // rotate the valid and data vectors from incoming channel
   wire [num_in_p-1:0] 		    valid_head_o_pretrunc;

   bsg_rotate_right #(.width_p(num_in_p)) valid_rr (.clk_i(clk), .data_i(valid_i), .rot_i(iptr_r), .o(valid_head_o_pretrunc));

   wire [2*width_p*num_in_p-1:0] data_head_o_flat_pretrunc
                                 =  { 2 { data_i_flat } } >> (iptr_r_data*width_p);

   wire [num_in_p*2-1:0]         yumi_intermediate;

   // rotate the yumi and valid signal to account for round-robin
   if (num_in_p >= middle_meet_p)
     begin
        assign  valid_head_o      =  valid_head_o_pretrunc    [0+:middle_meet_p        ];
        assign  data_head_o_flat  =  data_head_o_flat_pretrunc[0+:width_p*middle_meet_p];
        assign  yumi_intermediate =  { 2 { num_in_p ' (go_channels_i) } } << iptr_r;
     end
   else
     begin
        assign  valid_head_o      =  middle_meet_p      ' (valid_head_o_pretrunc    [0+:num_in_p             ]);
        assign  data_head_o_flat  =  middle_meet_data_lp ' (data_head_o_flat_pretrunc[0+:width_p*num_in_p]);
        assign  yumi_intermediate = { 2 { go_channels_i } } << iptr_r;
     end

   assign yumi_o             = yumi_intermediate[num_in_p +:num_in_p];

   bsg_circular_ptr #(.slots_p(num_in_p)
                      ,.max_add_p(min_in_middle_meet_p)
                      ) c_ptr
     (.reset_i(reset), .clk(clk)
      ,.add_i(go_cnt_i)
      ,.o(iptr_r)
      ,.n_o()
      );

   // we duplicate this logic for physical design because control and data do not always belong together
   bsg_circular_ptr #(.slots_p(num_in_p)
                      ,.max_add_p(min_in_middle_meet_p)
                      ) c_ptr_data
     (.reset_i(reset), .clk(clk)
      ,.add_i(go_cnt_i)
      ,.o(iptr_r_data)
      ,.n_o()
      );

endmodule
