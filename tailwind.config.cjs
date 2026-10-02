module.exports = {
  mode: 'jit',
  purge: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: { extend: { colors: { cloud: '#85ded4' } } },
  corePlugins: { preflight: false },
  plugins: [],
};
