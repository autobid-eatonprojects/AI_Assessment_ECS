import type { ExtractedSchedule } from "@/lib/types";

export function ExtractedScheduleTable({ schedule }: { schedule: ExtractedSchedule }) {
  return (
    <div className="space-y-2 rounded-md border bg-card">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <h4 className="font-medium">{schedule.name}</h4>
        <span className="text-xs text-muted-foreground">
          {schedule.rows.length} {schedule.rows.length === 1 ? "row" : "rows"}
        </span>
      </div>
      <div className="overflow-x-auto px-3 pb-3">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b">
              {schedule.columns.map((col, i) => (
                <th
                  key={`${col}-${i}`}
                  className="px-2 py-1.5 text-left font-medium text-muted-foreground"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {schedule.rows.map((row, ri) => (
              <tr
                key={ri}
                className="border-b last:border-b-0 hover:bg-muted/40"
              >
                {schedule.columns.map((col, ci) => {
                  const v = row[col];
                  return (
                    <td key={ci} className="px-2 py-1.5 align-top">
                      {v == null
                        ? ""
                        : typeof v === "object"
                          ? JSON.stringify(v)
                          : String(v)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
