export type ChartPoint = { t: number; v: number };

type LineChartProps = {
  title: string;
  points: ChartPoint[];
  /** Step lines suit discrete values like stock status. */
  step?: boolean;
  /** Custom y-axis tick labels (value → label); defaults to numeric ticks. */
  yTicks?: { value: number; label: string }[];
  formatValue?: (value: number) => string;
  color?: string;
};

const W = 640;
const H = 220;
const PAD = { top: 16, right: 16, bottom: 28, left: 56 };

function fmtDate(t: number): string {
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Dependency-free responsive SVG line chart with an accessible text summary. */
export function LineChart({
  title,
  points,
  step = false,
  yTicks,
  formatValue = (v) => String(v),
  color = "#2563eb",
}: LineChartProps) {
  if (points.length === 0) return null;

  const sorted = [...points].sort((a, b) => a.t - b.t);
  const ts = sorted.map((p) => p.t);
  const vs = sorted.map((p) => p.v);
  const minT = Math.min(...ts);
  const maxT = Math.max(...ts);
  let minV = yTicks ? Math.min(...yTicks.map((t) => t.value)) : Math.min(...vs);
  let maxV = yTicks ? Math.max(...yTicks.map((t) => t.value)) : Math.max(...vs);
  if (minV === maxV) {
    minV -= 1;
    maxV += 1;
  }

  const x = (t: number) =>
    PAD.left + (maxT === minT ? 0.5 : (t - minT) / (maxT - minT)) * (W - PAD.left - PAD.right);
  const y = (v: number) =>
    PAD.top + (1 - (v - minV) / (maxV - minV)) * (H - PAD.top - PAD.bottom);

  let d = "";
  sorted.forEach((p, i) => {
    if (i === 0) d += `M${x(p.t)},${y(p.v)}`;
    else if (step) d += ` H${x(p.t)} V${y(p.v)}`;
    else d += ` L${x(p.t)},${y(p.v)}`;
  });

  const ticks =
    yTicks ??
    [0, 1, 2, 3].map((i) => {
      const value = minV + ((maxV - minV) * i) / 3;
      return { value, label: formatValue(Number(value.toFixed(2))) };
    });

  const last = sorted[sorted.length - 1];
  const summary = `${title}: ${sorted.length} observations from ${fmtDate(minT)} to ${fmtDate(
    maxT,
  )}; latest ${formatValue(last.v)}.`;

  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      <svg aria-label={summary} role="img" viewBox={`0 0 ${W} ${H}`}>
        {ticks.map((tick) => (
          <g key={tick.label}>
            <line className="chart-grid" x1={PAD.left} x2={W - PAD.right} y1={y(tick.value)} y2={y(tick.value)} />
            <text className="chart-label" textAnchor="end" x={PAD.left - 8} y={y(tick.value) + 4}>
              {tick.label}
            </text>
          </g>
        ))}
        <text className="chart-label" textAnchor="start" x={PAD.left} y={H - 6}>
          {fmtDate(minT)}
        </text>
        <text className="chart-label" textAnchor="end" x={W - PAD.right} y={H - 6}>
          {fmtDate(maxT)}
        </text>
        <path d={d} fill="none" stroke={color} strokeLinejoin="round" strokeWidth={2} />
        {sorted.map((p) => (
          <circle cx={x(p.t)} cy={y(p.v)} fill={color} key={`${p.t}-${p.v}`} r={3}>
            <title>{`${new Date(p.t).toLocaleString()}: ${formatValue(p.v)}`}</title>
          </circle>
        ))}
      </svg>
    </figure>
  );
}
