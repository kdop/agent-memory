<script setup>
// Root component. All real UI lives under MainLayout (see src/router.js).
</script>

<template>
  <router-view />
</template>

<style>
/* Larger base than Quasar's 14px default — nudges the whole UI up. */
html {
  font-size: 16.5px;
}

/* In dark mode, soften text to a slightly off-white so it doesn't glare against
   the dark background (lower contrast, easier on the eyes). */
.body--dark {
  color: #d6d3cb;
}
.body--dark .text-grey,
.body--dark .q-item__label--caption {
  color: #9a978f !important;
}

/* The memories q-table renders small + high-contrast by default — bump the cell
   text up and, in dark mode, force the same soft off-white as the rest of the UI
   (Quasar sets an explicit near-white on table cells that ignores the body color). */
.q-table tbody td,
.q-table thead th,
.mem-content,
.mem-snippet {
  font-size: 0.95rem;
  line-height: 1.45;
}
.body--dark .q-table tbody td {
  color: #d6d3cb;
}
.body--dark .q-table thead th {
  color: #b4b1a9;
}
</style>
