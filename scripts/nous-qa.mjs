#!/usr/bin/env node

import { main } from '../tests/e2e/qa/cli.mjs';

const exitCode = await main(process.argv.slice(2), process.env);
process.exitCode = exitCode;
