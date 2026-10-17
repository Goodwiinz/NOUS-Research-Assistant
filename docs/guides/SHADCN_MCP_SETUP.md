# shadcn/ui MCP Server Integration

> **Dated 2026-10-17: IDE and MCP setup steps depend on third-party tools and on a local configuration that this repository does not commit. Treat it as a setup note, not a verified procedure. The Claude Code and Cursor variants ([CLAUDE_CODE_SHADCN_SETUP.md](CLAUDE_CODE_SHADCN_SETUP.md), [CURSOR_SHADCN_SETUP.md](CURSOR_SHADCN_SETUP.md)) are superseded by this guide.**

This project is configured with shadcn/ui MCP Server integration for seamless component access. You have **both** Claude Code MCP integration and VS Code extension options available!

## ✅ ALREADY CONFIGURED: Claude Code MCP Integration

Your project already has shadcn/ui MCP server configured:
- **`.mcp.json`** - MCP server configuration for Claude Code
- **shadcn skill** - Available in Claude Code with intelligent triggers
- **React framework** - Optimized for your Next.js project

### 🎯 Quick Start with Claude Code

1. **Configure GitHub Token** (one-time setup):
   - Go to [GitHub Settings > Developer settings > Personal access tokens](https://github.com/settings/tokens)
   - Create token with `public_repo` scope
   - Edit `.mcp.json` and replace `YOUR_GITHUB_TOKEN_HERE`

2. **Start using immediately**:
   ```bash
   # You can now use shadcn commands directly:
   /skill:shadcn "Show me the button component"
   /skill:shadcn "Add a card component to my project"
   ```

## 🚀 Additional Setup: VS Code Extension Integration

If you prefer VS Code extensions, you can also set up:

#### Option A: VS Code Settings (Recommended)
Edit `.vscode/settings.json`:
```json
{
  "continue.server": {
    "mcpServers": {
      "shadcn-ui": {
        "command": "npx",
        "args": [
          "@jpisnice/shadcn-ui-mcp-server",
          "--framework",
          "react",
          "--github-api-key",
          "YOUR_ACTUAL_GITHUB_TOKEN"
        ]
      }
    }
  }
}
```

#### Option B: Environment Variable
Edit `.env.local`:
```
GITHUB_PERSONAL_ACCESS_TOKEN=YOUR_ACTUAL_GITHUB_TOKEN
```

Then update `.vscode/settings.json` to use the environment variable:
```json
{
  "continue.server": {
    "mcpServers": {
      "shadcn-ui": {
        "command": "npx",
        "args": [
          "@jpisnice/shadcn-ui-mcp-server",
          "--framework",
          "react"
        ],
        "env": {
          "GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_PERSONAL_ACCESS_TOKEN}"
        }
      }
    }
  }
}
```

## 🎯 Usage Examples

### With Continue Extension

1. **Open Continue Chat** (Ctrl+Shift+L)
2. **Ask for components**:
   ```
   "Show me the shadcn/ui button component"
   "Get the card component implementation"
   "List all available shadcn/ui components"
   "Show me how to use the dialog component"
   "Get a dashboard layout example"
   ```

### Example Prompts

#### Component Discovery:
```
"List all available shadcn/ui components"
"Show me the button component source code"
"What's the difference between Alert and AlertDialog?"
```

#### Component Implementation:
```
"Add a shadcn/ui button to my project"
"Create a form with shadcn/ui input and label components"
"Show me how to use the Table component"
```

#### Customization:
```
"How can I customize the button component?"
"Show theming examples for the Card component"
"What variants are available for the Select component?"
```

## 🔧 Project-Specific Configuration

This project is pre-configured for React with:

- **Framework**: React/Next.js
- **UI Library**: shadcn/ui + Radix UI
- **Styling**: Tailwind CSS
- **Components**: Already installed many shadcn/ui components

### Available Components in This Project

Your project already has these shadcn/ui components:
- Accordion
- Alert Dialog
- Avatar
- Checkbox
- Dialog
- Dropdown Menu
- Label
- Popover
- Progress
- Radio Group
- Scroll Area
- Select
- Separator
- Slider
- Switch
- Tabs
- Tooltip

## 🐛 Troubleshooting

### Extension Not Working

1. **Check if MCP server runs standalone**:
   ```bash
   cd frontend
   npx @jpisnice/shadcn-ui-mcp-server --help
   ```

2. **Verify npx is available**:
   ```bash
   npx --version
   ```

3. **Restart VS Code** after configuration changes

4. **Check extension logs**:
   - Open Output panel (View → Output)
   - Select "Continue" from dropdown

### Common Issues

**"Command not found"**:
- Ensure Node.js and npm are installed
- Check that npx is available: `npx --version`

**"Rate limit exceeded"**:
- Add your GitHub token to the configuration
- Make sure the token has the required scopes

**"Extension not recognizing MCP server"**:
- Restart VS Code
- Check JSON syntax in settings.json
- Verify the token is correctly set

**Permission Issues**:
- Ensure your GitHub token has `public_repo` scope
- Token should not have expiration date set to "none"

## 🚀 Next Steps

1. **Configure your GitHub token** in both `.vscode/settings.json` and `.env.local`
2. **Install Continue extension** in VS Code
3. **Test the integration** with example prompts
4. **Start building** with seamless shadcn/ui component access!

## 📚 Additional Resources

- [Continue Extension Documentation](https://docs.continue.dev/)
- [shadcn/ui Documentation](https://ui.shadcn.com/)
- [MCP Server Documentation](https://github.com/jpisnice/shadcn-ui-mcp-server)
- [Project-specific Components](../../frontend/src/components/ui/)