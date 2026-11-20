import { Focusable, Navigation, findClassByName } from '@decky/ui';
import { FunctionComponent, useRef } from 'react';
import ReactMarkdown, { Options as ReactMarkdownOptions } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { UI_COLORS } from '../utils/uiStyles';

interface MarkdownProps extends ReactMarkdownOptions {
  onDismiss?: () => void;
}

const Markdown: FunctionComponent<MarkdownProps> = (props) => {
  const eventDetailsBodyClassName = findClassByName('EventDetailsBody') || undefined;

  return (
    <Focusable>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          div: (nodeProps: any) => <Focusable {...nodeProps.node.properties}>{nodeProps.children}</Focusable>,
          a: (nodeProps: any) => {
            const aRef = useRef<HTMLAnchorElement>(null);
            return (
              // TODO fix focus ring
              <Focusable
                onActivate={() => {}}
                onOKButton={() => {
                  props.onDismiss?.();
                  Navigation.NavigateToExternalWeb(aRef.current!.href);
                }}
                style={{ display: 'inline' }}
                focusClassName="steam-focus"
                className={eventDetailsBodyClassName}
              >
                <a
                  ref={aRef}
                  {...nodeProps.node.properties}
                  style={{ color: UI_COLORS.accentBlueHi, textDecoration: 'underline' }}
                >
                  {nodeProps.children}
                </a>
              </Focusable>
            );
          },
        }}
        {...props}
      />
    </Focusable>
  );
};

export default Markdown;
