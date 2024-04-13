/** @type {import('tailwindcss').Config} */
module.exports = {
    content: [
        "./index.html",
        "./src/**/*.vue",
    ],
    theme: {
        extend: {
            colors: {
                // perl bush
                'perl-bush': {
                    '50': '#f9f7f3',
                    '100': '#eae5d9', // sfondo
                    '200': '#e0d8c8',
                    '300': '#ccbea5',
                    '400': '#b7a080',
                    '500': '#a88b67',
                    '600': '#9b7a5b',
                    '700': '#81634d',
                    '800': '#6a5142',
                    '900': '#564438',
                    '950': '#2e221c',
                },
                'old-copper': {
                    '50': '#f7f4ef',
                    '100': '#ebe6d6',
                    '200': '#d9ccaf',
                    '300': '#c3ad81',
                    '400': '#b2915d',
                    '500': '#a37e4f',
                    '600': '#8b6643',
                    '700': '#7b563d', // colore
                    '800': '#5f4334',
                    '900': '#533a30',
                    '950': '#2f1f19',
                },
                'cedar': {
                    '50': '#fbf6f1',
                    '100': '#f6eade',
                    '200': '#ebd2bd',
                    '300': '#dfb292',
                    '400': '#d18c66',
                    '500': '#c77048',
                    '600': '#b95c3d',
                    '700': '#9a4834',
                    '800': '#7c3c30',
                    '900': '#64342a',
                    '950': '#421e19', // colore
                },
                'claret': {
                    '50': '#fef2f2',
                    '100': '#fee5e5',
                    '200': '#fccfd1',
                    '300': '#f9a8ab',
                    '400': '#f5777e',
                    '500': '#ec4754',
                    '600': '#d8263d',
                    '700': '#b61a32',
                    '800': '#991831',
                    '900': '#76162b', // accento
                    '950': '#490814',
                },
                'spectra': {
                    '50': '#f3f8f7',
                    '100': '#e0edec',
                    '200': '#c4dddc',
                    '300': '#9ac6c5',
                    '400': '#6aa6a6',
                    '500': '#4f8b8b',
                    '600': '#447376',
                    '700': '#3c5f62',
                    '800': '#3a5558', // accento
                    '900': '#314548',
                    '950': '#1d2d2f',
                },
                'asphalt': {
                    '50': '#f5f3f1',
                    '100': '#e7dfda',
                    '200': '#d0bfb8',
                    '300': '#b5998f',
                    '400': '#9f7b70',
                    '500': '#906a62',
                    '600': '#7b5753',
                    '700': '#644544',
                    '800': '#563d3e',
                    '900': '#4b383a',
                    '950': '#181112', // testo
                },
            }
        },
    },
    plugins: [
        // require('@tailwindcss/forms')({ strategy: "base" }),
    ],
}

