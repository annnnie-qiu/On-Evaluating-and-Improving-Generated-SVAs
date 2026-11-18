module bsg_fifo_1r1w_multi_fifo_head
  #(parameter lg_total_els_lp = 8)
   (input clk_i
    ,input reset_i

    ,input enque_i
    ,input [lg_total_els_lp-1:0] enque_addr_i

    ,output enque_addr_v_o
    ,output [lg_total_els_lp-1:0] enque_addr_o

    ,input deque_i

    // this is whether we should read from pointer storage
    ,output deque_addr_v_o 

    // this is the address to read from the fifo storage
    // and if applicable, the address to read from pointer storage
    ,output [lg_total_els_lp-1:0] deque_addr_o

    // coming back from memory
    ,input  [lg_total_els_lp-1:0] deque_data_i

    ,output v_o
    );

   logic [lg_total_els_lp-1:0] tail_r, head_r, head_r_bypass;
   logic 		       v_r, v_n;

   assign enque_addr_v_o = v_r & enque_i;
   assign enque_addr_o   = tail_r;

   assign v_o = v_r;

   wire equal = (tail_r == head_r_bypass);

   always_comb
     begin
	v_n = v_r;
	if (equal & deque_i) v_n = 1'b0; // singleton deque -> empty
	if (enque_i) v_n = 1'b1; // enq overrides
     end

   always_ff @(posedge clk_i)
     if (reset_i) v_r <= 1'b0;
     else         v_r <= v_n;

   always_ff @(posedge clk_i)
     if (reset_i)      tail_r <= '0;
     else if (enque_i) tail_r <= enque_addr_i;

   // only get next pointer if we have more than one element
   wire deque_addr_v_lo = deque_i & v_r & ~equal;
   assign deque_addr_v_o = deque_addr_v_lo;

   logic outstanding_read_r;

   always_ff @(posedge clk_i)
     begin
	if (reset_i) outstanding_read_r <= 1'b0;
	else         outstanding_read_r <= deque_addr_v_lo;
     end

   always_ff @(posedge clk_i)
     // if we have fresh meat
     if (reset_i) head_r <= '0;
     else if (enque_i & (~v_r | (equal & deque_i))) head_r <= enque_addr_i; // empt enq, singleton enq+deq: replace element
     else if (outstanding_read_r)                  head_r <= deque_data_i;

   assign head_r_bypass = outstanding_read_r ? deque_data_i : head_r;

   // need to bypass data coming in
   assign deque_addr_o = head_r_bypass;

endmodule
