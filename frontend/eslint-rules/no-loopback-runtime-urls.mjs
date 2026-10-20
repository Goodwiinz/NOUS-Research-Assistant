const loopbackUrl =
  /(?:https?|wss?):\/\/(?:[^/@\s]+@)?(?:(?:[a-z0-9-]+\.)*localhost\.?|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])(?=[:/?#]|$)/i;

export default {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Keep loopback URL defaults in the shared endpoint resolver.',
    },
    schema: [],
    messages: {
      loopback:
        'Use the shared publicEndpoints resolver; runtime code must not hardcode loopback URLs.',
    },
  },
  create(context) {
    function check(node, value) {
      if (typeof value === 'string' && loopbackUrl.test(value)) {
        context.report({ node, messageId: 'loopback' });
      }
    }
    return {
      Literal(node) {
        check(node, node.value);
      },
      TemplateElement(node) {
        check(node, node.value.cooked ?? node.value.raw);
      },
    };
  },
};
