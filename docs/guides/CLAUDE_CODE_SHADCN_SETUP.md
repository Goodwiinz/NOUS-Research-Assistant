# Claude Code shadcn/ui MCP Server Integration

> **Dated 2026-10-17: this is a one-off IDE setup status note kept for reference. The consolidated shadcn/ui MCP setup guide is [SHADCN_MCP_SETUP.md](SHADCN_MCP_SETUP.md); prefer it. Check the tool's current documentation before running any command here.**

## ✅ Setup Complete

Your Claude Code has been successfully configured with the **official** shadcn/ui MCP Server! Here's what was configured:

### Configuration Details
- **Editor**: Claude Code (claude.ai/code)
- **MCP Server**: Official shadcn MCP Server (`shadcn@latest mcp`)
- **Registry**: Official shadcn/ui registry with component browsing and installation
- **Authentication**: Uses your project's components.json configuration

### What Was Set Up

1. **MCP Configuration File**:
   - Created `/Users/goodwiinz/.claude/plugins/repos/shadcn-ui-mcp/.mcp.json`
   - Uses official shadcn CLI: `npx shadcn@latest mcp`

2. **Plugin Registration**:
   - Updated `/Users/goodwiinz/.claude/plugins/config.json` to include the shadcn-ui-mcp repository
   - Claude Code will now recognize and load the MCP server

3. **Permissions**:
   - Updated in your project settings to use the official command

### How to Use

The official shadcn/ui MCP Server is now available whenever you use Claude Code in your project. You can:

1. **Browse and search components**:
   ```
   "Show me all available components"
   "Find me a login form"
   "What form components are available?"
   ```

2. **Install components directly**:
   ```
   "Add the button component"
   "Install the card component"
   "Create a login form using shadcn components"
   ```

3. **Get component information**:
   ```
   "Show me the button component details"
   "Get usage examples for the dialog"
   "How do I configure the table component?"
   ```

### Available Components

The official shadcn MCP server provides access to the complete shadcn/ui registry:
- **All Official Components**: Button, Input, Card, Dialog, Table, and more
- **Forms & Validation**: Complete form components with built-in validation
- **Layouts & Templates**: Pre-built dashboard layouts, forms, and UI blocks
- **Themes & Styling**: Full theme support with Tailwind CSS integration
- **Component Variants**: Multiple variants and sizes for each component
- **Installation**: Direct component installation to your project

**Key advantage**: You can now both browse AND install components directly through Claude Code!

### Integration with Your RAG System

Perfect for your RAG system frontend:
- **Document Upload UI**: Use Card, Button, Progress components
- **Search Interface**: Input, Button, Badge components for search results
- **Real-time Status**: Skeleton, Progress, Alert components for processing status
- **Dashboard Layout**: Navigation, Sidebar, Card components for admin panels
- **Settings Pages**: Form components with validation

### Configuration Files

**MCP Server Configuration**:
```json
// /Users/goodwiinz/.claude/plugins/repos/shadcn-ui-mcp/.mcp.json
{
  "mcpServers": {
    "shadcn": {
      "command": "npx",
      "args": ["shadcn@latest", "mcp"]
    }
  }
}
```

**Plugin Registration**:
```json
// /Users/goodwiinz/.claude/plugins/config.json
{
  "repositories": {
    "shadcn-ui-mcp": {
      "path": "/Users/goodwiinz/.claude/plugins/repos/shadcn-ui-mcp"
    }
  }
}
```

**Optional: components.json** (for private registries):
```json
{
  "style": "default",
  "rsc": true,
  "tsx": true,
  "tailwind": {
    "config": "tailwind.config.js",
    "css": "app/globals.css",
    "baseColor": "slate",
    "cssVariables": true
  },
  "aliases": {
    "components": "@/components",
    "utils": "@/lib/utils"
  },
  "registries": {
    "@acme": "https://acme.com/{name}.json",
    "@internal": {
      "url": "https://internal.company.com/{name}.json",
      "headers": {
        "Authorization": "Bearer ${REGISTRY_TOKEN}"
      }
    }
  }
}
```

### Troubleshooting

**If components aren't available:**
1. Check that the MCP server starts correctly:
   ```bash
   npx shadcn@latest mcp --help
   ```

2. Verify you have a working components.json file in your project

3. Check Claude Code permissions include the MCP server:
   - Should be in your `.claude/settings.local.json`: `Bash(npx shadcn@latest:*)`

4. Restart Claude Code and try again

**Common Issues:**
- **Missing components.json**: Run `npx shadcn@latest init` to initialize
- **Network issues**: Ensure internet connectivity for registry access
- **Registry tokens**: Set environment variables for private registries

### Advanced Usage

**Private Registries**: Configure private component registries in components.json:
```json
{
  "registries": {
    "@internal": {
      "url": "https://internal.company.com/{name}.json",
      "headers": {
        "Authorization": "Bearer ${REGISTRY_TOKEN}"
      }
    }
  }
}
```

**Environment Variables**: Set up authentication for private registries:
```bash
# .env.local
REGISTRY_TOKEN=your_token_here
API_KEY=your_api_key_here
```

**Multiple Projects**: The MCP server works project-wide and respects each project's components.json configuration.

### Example Interactions

**For your RAG System**:
```
"Add a card component for displaying document metadata"
"Install a button component with loading state for file uploads"
"Create a progress component for real-time processing status"
"Build a form component for document search with filters"
```

**UI Development**:
```
"Show me all available dashboard layouts"
"Install a navigation menu with search functionality"
"Add dialog components for confirmations"
"Create a table with sorting and pagination"
```

### Key Benefits of Official Integration

✅ **Direct Installation**: Install components directly to your project
✅ **Official Registry**: Access to the latest official shadcn/ui components
✅ **No Authentication Required**: Works with public registry out of the box
✅ **Project-Specific**: Respects your project's components.json configuration
✅ **Private Registry Support**: Configure private component registries

### Next Steps

1. **Initialize shadcn/ui** in your frontend project if not already done:
   ```bash
   cd frontend && npx shadcn@latest init
   ```

2. **Start installing components** through Claude Code:
   - "Add the button component"
   - "Install a card component for documents"
   - "Create a complete login form"

3. **Explore the component library**:
   - "Show me all available components"
   - "Find form components"
   - "What dashboard layouts are available?"

The integration is seamless and ready to use! You now have access to the entire official shadcn/ui component library with direct installation capabilities through Claude Code. 🚀

### Related Documentation

- [Official shadcn/ui MCP Docs](https://ui.shadcn.com/docs/mcp) - Official MCP documentation
- [shadcn/ui Documentation](https://ui.shadcn.com/) - Official component docs
- [Cursor Setup Guide](./CURSOR_SHADCN_SETUP.md) - For Cursor IDE integration
- [Project Documentation](../README.md) - Your RAG system docs