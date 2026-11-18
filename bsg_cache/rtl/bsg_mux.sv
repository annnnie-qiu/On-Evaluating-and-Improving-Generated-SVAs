
module bsg_mux #(parameter width_p = 10
                 ,parameter els_p=1
                 ,parameter harden_p = 0
                 ,parameter balanced_p = 0
                 ,parameter lg_els_lp=( (((els_p)==1) || ((els_p)==0))? 1 : $clog2((els_p)))
                 )
   (
    
    input clk_i,
input [els_p-1:0][width_p-1:0] data_i
    ,input [lg_els_lp-1:0] sel_i
    ,output [width_p-1:0] data_o
    );

   if (els_p == 1)
     begin
      assign data_o = data_i;
      wire unused = sel_i;
     end
   else
     assign data_o = data_i[sel_i];

endmodule
