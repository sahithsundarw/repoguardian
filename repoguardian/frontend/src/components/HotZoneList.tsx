import React from "react";
import type { HotZone } from "../api/client";
import { T } from "../theme";

interface Props {
  zones: HotZone[];
}

export const HotZoneList: React.FC<Props> = ({ zones }) => {
  const maxRisk = Math.max(...zones.map((z) => z.risk_score), 1);

  return (
    <div style={{ background: T.surface, borderRadius: 16, padding: 24 }}>
      <h3 style={{ color: T.text, margin: "0 0 16px", fontSize: 16 }}>
        🔥 Risk Hot Zones
      </h3>
      {zones.length === 0 && (
        <div style={{ color: T.textDim }}>No hot zones detected.</div>
      )}
      {zones.map((zone, i) => {
        const pct = (zone.risk_score / maxRisk) * 100;
        const barColor =
          zone.critical_count > 0 ? T.critical
          : zone.high_count > 0 ? T.high
          : T.medium;

        return (
          <div key={i} style={{ marginBottom: 14 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span style={{ color: T.text, fontSize: 12, fontFamily: "monospace" }}>
                {zone.file_path}
              </span>
              <span style={{ color: T.textMuted, fontSize: 11 }}>
                {zone.finding_count} findings
                {zone.critical_count > 0 && <span style={{ color: T.critical, marginLeft: 6 }}>●{zone.critical_count}C</span>}
                {zone.high_count > 0 && <span style={{ color: T.high, marginLeft: 4 }}>●{zone.high_count}H</span>}
              </span>
            </div>
            <div style={{ background: T.bg, borderRadius: 4, height: 6, overflow: "hidden" }}>
              <div style={{
                width: `${pct}%`, height: "100%",
                background: barColor,
                borderRadius: 4,
                transition: "width 0.3s",
              }} />
            </div>
          </div>
        );
      })}
    </div>
  );
};
