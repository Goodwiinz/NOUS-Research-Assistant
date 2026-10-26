# Cursor shadcn/ui MCP Server Integration

> **Dated 2026-10-17: this is a one-off IDE setup status note kept for reference. The consolidated shadcn/ui MCP setup guide is [SHADCN_MCP_SETUP.md](SHADCN_MCP_SETUP.md); prefer it. Check the tool's current documentation before running any command here.**

## ✅ Setup Complete

Your Cursor has been successfully configured with the **official** shadcn/ui MCP Server! Here's what was configured:

### Configuration Details
- **Editor**: Cursor
- **Extension**: Continue (already installed)
- **MCP Server**: Official shadcn MCP Server (`shadcn@latest mcp`)
- **Registry**: Official shadcn/ui registry with component browsing and installation
- **Framework**: React (perfect for your RAG system frontend)

### How to Use

1. **Restart Cursor** completely (quit and reopen)
2. **Open Continue Chat** (Cmd+Shift+L or View → Command Palette → "Continue: New Chat")
3. **Try these commands**:
   ```
   "Show me all available components"
   "Add the button component"
   "Find a login form"
   "Install a card component for my RAG frontend"
   ```

### Available Features
The official shadcn MCP server provides:
- **Component Browsing**: Search and explore all shadcn/ui components
- **Direct Installation**: Install components directly into your project
- **Usage Examples**: Get component implementation examples
- **Complete Forms & Layouts**: Pre-built forms, dashboards, and UI blocks
- **Theme Support**: Full Tailwind CSS integration

### Configuration File Location
Your Cursor settings were updated at:
```
~/Library/Application Support/Cursor/User/settings.json
```

### Prerequisites for Your Project

For the best experience, make sure your frontend project has shadcn/ui initialized:

```bash
cd frontend
npx shadcn@latest init
```

This creates a `components.json` file that the MCP server will use for configuration.

### Troubleshooting

**If it doesn't work after restart:**
1. Check that Cursor can access the MCP server by running:
   ```bash
   npx shadcn@latest mcp --help
   ```

2. Check Continue extension logs in Cursor:
   - Open Output panel (View → Output)
   - Select "Continue" from dropdown

3. Verify your frontend project has a `components.json` file

**Common Issues:**
- Ensure you have internet connection for registry access
- Make sure Node.js and npm are installed and accessible
- Check that your project has shadcn/ui initialized (`npx shadcn@latest init`)

### Key Benefits
✅ **Direct Installation**: Install components directly into your frontend project
✅ **Official Components**: Access to the latest shadcn/ui registry
✅ **No Setup Required**: Works with public registry immediately
✅ **Project Integration**: Respects your project's components.json

### Next Steps
- Initialize shadcn/ui in your frontend if not already done
- Start installing components: "Add the button component"
- Browse available components: "Show me all available components"
- Build complete UI blocks: "Create a login form"

Happy coding with the official shadcn/ui integration! 🚀