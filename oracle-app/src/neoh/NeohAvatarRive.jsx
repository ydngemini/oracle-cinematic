/**
 * NeohAvatarRive — the seam the final animated character drops into.
 *
 * This module is imported LAZILY and only when `VITE_NEOH_AVATAR_RIVE` names
 * a real asset (see NeohAvatar.jsx). With no asset configured it is never
 * imported, never bundled, and the Rive runtime is not a dependency — which
 * is why the bundle budget is unchanged by this work.
 *
 * ── Enabling it, once a .riv exists ──────────────────────────────────────
 *
 *   1. npm i @rive-app/react-canvas
 *   2. drop the asset at oracle-app/public/neoh/neoh-avatar.riv
 *   3. set VITE_NEOH_AVATAR_RIVE=/neoh/neoh-avatar.riv
 *   4. uncomment the block below
 *
 * No caller changes. The rest of the product keeps speaking product states.
 * The numeric input contract lives in riveInputs.js, next to this file.
 *
 * Amplitude is deliberately pushed onto the Rive instance imperatively
 * rather than through React state: sixty state updates a second would cost
 * more than the animation it is driving.
 */

import { useEffect } from 'react';

/**
 * Until the asset and runtime exist this reports failure once, so
 * NeohAvatar falls back to its inline SVG face permanently rather than
 * suspending forever. That is the honest behaviour: there is no asset, so
 * there is nothing to render here yet.
 */
export default function NeohAvatarRive({ onError }) {
  useEffect(() => {
    onError?.(new Error('Neoh Rive asset is not configured'));
  }, [onError]);
  return null;

  /* ── Real implementation, once the asset lands ─────────────────────────
  const { rive, RiveComponent } = useRive({
    src,
    stateMachines: 'NeohState',
    autoplay: true,
    onLoadError: onError,
  });
  const stateIn = useStateMachineInput(rive, 'NeohState', 'state');
  const levelIn = useStateMachineInput(rive, 'NeohState', 'level');
  const actionIn = useStateMachineInput(rive, 'NeohState', 'actionType');
  const attentionIn = useStateMachineInput(rive, 'NeohState', 'attention');
  const stillIn = useStateMachineInput(rive, 'NeohState', 'still');

  useEffect(() => { if (stateIn) stateIn.value = stateInput(state); }, [state, stateIn]);
  useEffect(() => { if (actionIn) actionIn.value = actionInput(actionType); }, [actionType, actionIn]);
  useEffect(() => { if (attentionIn) attentionIn.value = attentionLevel; }, [attentionLevel, attentionIn]);
  useEffect(() => { if (stillIn) stillIn.value = Boolean(still); }, [still, stillIn]);
  useEffect(() => { if (levelIn) levelIn.value = audioLevel; }, [audioLevel, levelIn]);

  return <RiveComponent className={className} />;
  ────────────────────────────────────────────────────────────────────── */
}
