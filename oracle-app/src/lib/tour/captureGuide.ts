/**
 * How to capture a property for Neoh Space, in the order someone actually
 * does it. Each line corresponds to a way real captures fail (blur from quick
 * turns, rooms that never connect, dark frames) — not generic photo advice —
 * and none of it names a tool, a format or an algorithm.
 */
export interface CaptureStep {
  title: string;
  text: string;
}

export const CAPTURE_STEPS: CaptureStep[] = [
  { title: 'Start', text: 'Turn on every light and open the blinds. Start in a doorway with the phone at chest height.' },
  { title: 'Move through the property', text: 'Walk slowly and keep the phone level. Turn gently — quick turns blur every frame.' },
  { title: 'Cover everything', text: 'Finish a full loop of each room, and walk slowly through each doorway so rooms connect.' },
  { title: 'Finish', text: 'Aim for 120+ photos or a 2–4 minute video per floor. Avoid people, mirrors and shooting straight at windows.' },
  { title: 'Upload', text: 'Upload the photos or video above. Files that fail can be retried on their own — nothing already uploaded is lost.' },
];
