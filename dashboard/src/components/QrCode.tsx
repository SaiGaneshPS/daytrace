// DT-32: a pairing QR code, drawn by the hub (GET /pair/qr.png, hub computer only), in a frame whose border runs
// down with the code's time left. The image changes with the code (a new code, a new picture), and a code that has
// run out shows no QR at all, so an old picture can never be scanned.
type Props = {
  /** GET /pair/qr.png?for=app|browser, with the code in the query so a new code loads a new picture. */
  src: string;
  alt: string;
  /** How much of the code's time is left, from 1 (new) to 0 (run out). */
  left: number;
};

export default function QrCode({ src, alt, left }: Props) {
  return (
    <figure className="qr">
      <svg className="qr-ring" viewBox="0 0 100 100" aria-hidden="true">
        <rect className="qr-ring-track" x="2" y="2" width="96" height="96" rx="10" pathLength={100} />
        <rect
          className="qr-ring-fill"
          x="2"
          y="2"
          width="96"
          height="96"
          rx="10"
          pathLength={100}
          strokeDasharray="100"
          strokeDashoffset={100 * (1 - Math.max(0, Math.min(1, left)))}
        />
      </svg>
      <img src={src} alt={alt} width={220} height={220} />
    </figure>
  );
}
