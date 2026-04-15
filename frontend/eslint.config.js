// ESLint v9 flat config — minimal setup for React + TypeScript + Vite.
// Migrated from the implicit legacy .eslintrc since the repo only declares
// eslint-plugin-react-hooks + eslint-plugin-react-refresh + typescript-eslint.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";
import globals from "globals";

export default tseslint.config(
  {
    ignores: ["dist", "build", "node_modules", "coverage", "*.config.js", "*.config.ts"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    // Don't flag eslint-disable directives that are now redundant — the rules
    // they target may have been turned off globally.
    linterOptions: { reportUnusedDisableDirectives: false },
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // Off project-wide: these are sessionStorage-sync effects and stable
      // setter functions we deliberately omit from deps. Toggling them on
      // would force code restructuring without any real safety win.
      "react-hooks/exhaustive-deps": "off",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
);
