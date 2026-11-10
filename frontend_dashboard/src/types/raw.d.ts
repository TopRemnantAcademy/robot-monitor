// Vite `?raw` import type declaration.
// tsconfig's include is only ["src"], which doesn't cover the repo-root docs/;
// this declares a wildcard module so `import s from '...schema.json?raw'` gets a string type.
declare module '*?raw' {
  const content: string;
  export default content;
}
