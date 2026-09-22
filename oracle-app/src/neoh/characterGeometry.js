/**
 * Where each variant puts the head, and how big its canvas is.
 *
 * Its own file because NeohCharacter.jsx may only export components — but it
 * belongs outside that file anyway: the tests, the stylesheet's width maths
 * and the Rive artboard spec all need these numbers, and none of them should
 * have to import a React component to read a viewBox.
 *
 * The head is always authored in its own 32x32 space and placed by transform,
 * so head geometry is written once and every variant inherits each fix to it.
 */
export const VARIANT_GEOMETRY = Object.freeze({
  head: Object.freeze({ viewBox: '0 0 32 32', headTransform: null, width: 32, height: 32 }),
  bust: Object.freeze({ viewBox: '0 0 40 44', headTransform: 'translate(6.88 1) scale(0.82)', width: 40, height: 44 }),
  full: Object.freeze({ viewBox: '0 0 40 72', headTransform: 'translate(8 1) scale(0.75)', width: 40, height: 72 }),
});

export const VARIANTS = Object.freeze(Object.keys(VARIANT_GEOMETRY));
