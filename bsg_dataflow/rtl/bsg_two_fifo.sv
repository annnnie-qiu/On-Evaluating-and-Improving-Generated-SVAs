// MBT 9-7-2014
//
// two element fifo
//
// helpful interface on both input and output
//
// input : ready/valid   flow control  (rv->&)
// output: valid->yumi   flow control ( v->r)
//
// see https://github.com/anonymized/basejump_stl/blob/master/docs/BaseJump_STL_DAC_2018_Camera_Ready.pdf
// to understand the flow control standards in BaseJump STL.
//
// INPUTS: although this module's inputs adheres to
// ready/valid protocol where both sender and receiver
// AND the two signals together to determine
// if transaction happened; in some cases, we
// know that the sender takes into account the
// ready signal before sending out valid, and the
// check is unnecessary. We use ready_THEN_valid_p
// to remove the check if it is unnecessary.
//
// fixme: should be renamed to bsg_fifo_two
// note: ~v_o == fifo is empty.
//

module bsg_two_fifo #(parameter width_p = 8
                      , parameter verbose_p=0
                      // whether we should allow simultaneous enque and deque on full
                      , parameter allow_enq_deq_on_full_p=0
                      // necessarily, if we allow enq on ready low, then
                      // we are not using a ready/valid protocol
                      , parameter ready_THEN_valid_p=allow_enq_deq_on_full_p
                      )
   (input clk_i
    , input reset_i

    // input side
    , output              ready_param_o // early
    , input [width_p-1:0] data_i  // late
    , input               v_i     // late

    // output side
    , output              v_o     // early
    , output[width_p-1:0] data_o  // early
    , input               yumi_i  // late
    );

   wire deq_i = yumi_i;
   wire enq_i;

   logic                  head_r,  tail_r;
   logic                  empty_r, full_r;

   bsg_mem_1r1w #(.width_p(width_p)
                  ,.els_p(2)
		  ,.read_write_same_addr_p(allow_enq_deq_on_full_p)
                  ) mem_1r1w
     (.w_clk_i   (clk_i  )
      ,.w_reset_i(reset_i)
      ,.w_v_i    (enq_i  )
      ,.w_addr_i (tail_r )
      ,.w_data_i (data_i )
      ,.r_v_i    (~empty_r)
      ,.r_addr_i (head_r )
      ,.r_data_o (data_o )
      );

   assign v_o       = ~empty_r;
   assign ready_param_o   = ~full_r;

   if (ready_THEN_valid_p)
     assign enq_i = v_i;
   else
     assign enq_i = v_i & ~full_r;

   always_ff @(posedge clk_i)
     begin
        if (reset_i)
          begin
             tail_r  <= 1'b0;
             head_r  <= 1'b0;
             empty_r <= 1'b1;
             full_r  <= 1'b0;
          end
        else
          begin
             if (enq_i)
               tail_r         <= ~tail_r;

             if (deq_i)
               head_r         <= ~head_r;

             // logic simplifies nicely for 2 element case
             empty_r             <= (   empty_r & ~enq_i)
                                    | (~full_r  &  deq_i & ~enq_i);

             if (allow_enq_deq_on_full_p)
               full_r              <= (  ~empty_r &  enq_i & ~deq_i)
                                      | ( full_r  & ~(deq_i^enq_i));
             else
               full_r              <= (  ~empty_r &  enq_i & ~deq_i)
                                      | ( full_r  & ~deq_i);
          end // else: !if(reset_i)
     end // always_ff @

endmodule
