module i2c_master_bit_ctrl_assertions (
    input logic        clk,
    input logic        rst,
    input logic        nReset,
    input logic        ena,
    input logic [15:0] cnt,
    input logic        scl_sync,
    input logic        clk_en,
    input logic        scl_oen,
    input logic        dscl_oen,
    input logic        sSCL,
    input logic        slave_wait,
    input logic        sSDA,
    input logic        dSDA,
    input logic        sta_condition,
    input logic        sto_condition,
    input logic        busy,
    input logic        sda_chk,
    input logic        sda_oen,
    input logic        al,
    input logic        dSCL,
    input logic        dout
);

  a_clk_enable_generation: assert property (@(posedge clk) disable iff (!nReset)
    (cnt == 16'h0 || !ena || scl_sync) |-> ##1 (clk_en == 1'b1)
  );

  a_slave_wait_detection: assert property (@(posedge clk) disable iff (!nReset)
    (scl_oen && !dscl_oen && !sSCL) |-> ##1 (slave_wait == 1'b1)
  );

  a_start_condition_detection: assert property (@(posedge clk) disable iff (!nReset)
    (!sSDA && dSDA && sSCL) |-> ##1 (sta_condition == 1'b1)
  );

  a_stop_condition_detection: assert property (@(posedge clk) disable iff (!nReset)
    (sSDA && !dSDA && sSCL) |-> ##1 (sto_condition == 1'b1)
  );

  a_busy_after_start: assert property (@(posedge clk) disable iff (!nReset)
    sta_condition |-> ##1 (busy == 1'b1)
  );

  a_idle_after_stop: assert property (@(posedge clk) disable iff (!nReset)
    sto_condition |-> ##1 (busy == 1'b0)
  );

  a_arbitration_lost_detection: assert property (@(posedge clk) disable iff (!nReset)
    (sda_chk && !sSDA && sda_oen) |-> ##1 (al == 1'b1)
  );

  a_data_output_capture: assert property (@(posedge clk) disable iff (!nReset)
    (sSCL && !dSCL) |-> ##1 (dout == sSDA)
  );

  a_start_condition_has_prior_falling_sda: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    sta_condition |-> $past(!sSDA && dSDA && sSCL)
  );

  a_stop_condition_has_prior_rising_sda: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    sto_condition |-> $past(sSDA && !dSDA && sSCL)
  );

  a_enabled_start_detection: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    (!sSDA && dSDA && sSCL && ena) |-> ##1 sta_condition
  );

  a_clock_reload_observation: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    (cnt == 16'h0) |-> aq_missing_clock_monitor.expected_clk_en
  );

  a_bus_start_observation: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    (!sSDA && dSDA && sSCL) |-> aq_missing_bus_monitor.start_seen
  );

  a_clock_reload_drives_enable_low: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    (cnt == 16'h0 || !ena || scl_sync) |-> ##1 (clk_en == 1'b0)
  );

  a_start_edge_keeps_start_low: assert property (
    @(posedge clk) disable iff (!nReset || rst)
    (!sSDA && dSDA && sSCL) |-> ##1 (sta_condition == 1'b0)
  );

endmodule
