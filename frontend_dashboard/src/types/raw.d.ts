// Vite `?raw` 导入的类型声明。
// tsconfig 的 include 只有 ["src"]，覆盖不到仓库根的 docs/；
// 这里声明通配模块，让 `import s from '...schema.json?raw'` 有 string 类型。
declare module '*?raw' {
  const content: string;
  export default content;
}
