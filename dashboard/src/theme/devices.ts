// DT-34: the colors of devices in charts (Today's device donut, the Insights device charts). Written as
// "var(--token)" like every chart color, so useEChart resolves them for the current scheme. And what each kind of
// device is called (the Devices and Privacy pages).

/** Each kind of device, in words. */
export const DEVICE_TYPE_LABELS: Record<string, string> = {
  windows: "Windows PC",
  macos: "Mac",
  android: "Android phone",
  ios: "iPhone",
  browser: "Browser extension",
  viewer: "Dashboard viewer",
};

/** Each kind of device's color, the same on every page. */
export const DEVICE_COLORS: Record<string, string> = {
  windows: "var(--cat-work)",
  macos: "var(--cat-study)",
  android: "var(--cat-comms)",
  ios: "var(--cat-social)",
};
const SPARE_DEVICE_COLORS = ["var(--cat-video)", "var(--cat-games)", "var(--cat-health)", "var(--cat-other)"];

/** A color for each device, in order: its kind's, unless a device before it has that color already (a second
 * phone, a second PC). Then, like a device of another kind (a browser), it gets a spare color that no device has
 * and no kind in the list owns, so the lines of a chart can always be told apart. */
export function deviceColors(types: (string | null | undefined)[]): string[] {
  const owned = new Set(types.map((type) => (type ? DEVICE_COLORS[type] : undefined)).filter(Boolean));
  const spares = [...SPARE_DEVICE_COLORS, ...Object.values(DEVICE_COLORS)];
  const used = new Set<string>();
  return types.map((type, index) => {
    const own = type ? DEVICE_COLORS[type] : undefined;
    const color =
      own && !used.has(own)
        ? own
        : (spares.find((spare) => !used.has(spare) && !owned.has(spare)) ?? spares.find((spare) => !used.has(spare)) ?? spares[index % spares.length]);
    used.add(color);
    return color;
  });
}
